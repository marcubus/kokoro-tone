"""The OpenAPI document is the contract (ADR-0018).

FastAPI already derives the paths, parameters, bodies and success models from
the route signatures. This module adds the two things the base generator cannot
know: the role that may call each operation, and the OpenAI error envelope for
every status the operation can return. The result is cached on the app exactly
as FastAPI's own `openapi()` would cache it.

The role extension is derived from `access.py`, so a route that is added
without a policy decision still gets an honest answer: every operation is
admin-only until an allowlist names it, and the public set is code.
"""

from __future__ import annotations

from typing import Any, Literal

from fastapi import FastAPI
from fastapi.openapi.utils import get_openapi
from pydantic import BaseModel, ConfigDict, Field

from . import __version__
from .access import ADMIN_ONLY_SLUGS, PUBLIC_SLUGS

_ERROR_DESCRIPTIONS: dict[int, str] = {
    400: "The request is invalid: a bad payload, tag, sense, mode or bound.",
    401: "Authentication failed. Every 401 is the same envelope and header.",
    403: "The key is recognised but its role may not call this operation.",
    404: "The named pronunciation or proposal does not exist.",
    409: "The operation conflicts with the row's state.",
    413: "The request body exceeds the deployment's byte limit.",
    429: "The caller is rate limited.",
    500: "The gateway could not complete the operation.",
    502: "The engine or the audio pipeline failed.",
}


class ErrorObject(BaseModel):
    """The fields every failure carries; `ambiguities` only from a dry run."""

    message: str
    type: str
    param: str | None = None
    code: str | None = None
    ambiguities: list[dict[str, Any]] | None = None


class ErrorEnvelope(BaseModel):
    """`errors.py`'s OpenAI-shaped body, unchanged."""

    error: ErrorObject


def _headers(*names: str) -> dict[str, dict[str, Any]]:
    return {
        name: {"description": f"The `{name}` response header.", "schema": {"type": "string"}}
        for name in names
    }


def error_responses(*statuses: int) -> dict[int | str, dict[str, Any]]:
    """The error envelope for each status an operation declares it can return."""
    responses: dict[int | str, dict[str, Any]] = {}
    for status in statuses:
        entry: dict[str, Any] = {
            "model": ErrorEnvelope,
            "description": _ERROR_DESCRIPTIONS[status],
        }
        if status == 401:
            entry["headers"] = _headers("WWW-Authenticate")
        if status == 403:
            entry["headers"] = _headers("WWW-Authenticate")
        responses[status] = entry
    return responses


class TurnView(BaseModel):
    text: str
    voice: str
    emotion: str | None = None
    rate: float | None = None
    filter: str | None = None
    gap_ms: int | None = None
    lead_ms: int | None = None


class AmbiguityView(BaseModel):
    word: str
    offset: int
    chosen: str
    options: list[str]
    says: list[str]


class PreparedView(BaseModel):
    """The render's own prepared view, returned by `dry_run=1`."""

    text: str
    turns: list[TurnView]
    voice: str
    instructions: str | None = None
    ambiguity: str
    guesses: list[str]
    ambiguities: list[AmbiguityView]
    passthrough: bool
    chars: int
    normalise: str


class PronunciationRow(BaseModel):
    """One rule, from any layer, with the id that deletion addresses."""

    model_config = ConfigDict(extra="allow")

    object: Literal["pronunciation"] = "pronunciation"
    id: str
    status: str
    match: str
    say: str
    source: Literal["custom", "learned", "defaults"]
    sense: str | None = None
    voice: str | None = None
    senses: list[dict[str, Any]] = Field(default_factory=list)


class ProposalRow(BaseModel):
    """One proposal row: its lifecycle is the resource's state."""

    model_config = ConfigDict(extra="allow")

    object: Literal["proposal"] = "proposal"
    id: str
    status: Literal["pending", "approved", "rejected"]
    word: str
    say: str
    sense: str | None = None
    context: str | None = None
    created_at: str
    created_by_role: str
    decided_at: str | None = None
    decided_by_role: str | None = None
    reason: str | None = None
    applied: bool = False


class PronunciationList(BaseModel):
    object: Literal["list"] = "list"
    data: list[PronunciationRow | ProposalRow]


class DeletedObject(BaseModel):
    object: Literal["pronunciation", "proposal"]
    id: str
    deleted: bool


class Model(BaseModel):
    id: str
    object: Literal["model"] = "model"
    created: int
    owned_by: str


class ModelList(BaseModel):
    object: Literal["list"] = "list"
    data: list[Model]


class Health(BaseModel):
    model_config = ConfigDict(extra="allow")

    status: Literal["ok", "degraded"]
    detail: str
    version: str
    model: str
    upstream: str
    rubberband: bool
    filters: list[str]


def _roles(slug: str) -> list[str]:
    """The minimum role that satisfies each operation (ADR-0016, ADR-0018)."""
    if slug in PUBLIC_SLUGS:
        return ["public"]
    if slug in ADMIN_ONLY_SLUGS:
        return ["admin"]
    return ["default", "admin"]


def build(app: FastAPI) -> dict[str, Any]:
    """Generate the document, then add roles and remove FastAPI's 422.

    The gateway answers a validation failure with `errors.py`'s 400, never
    FastAPI's 422 envelope, so a 422 in the spec would describe a response no
    caller can receive.
    """
    schema = get_openapi(
        title=app.title,
        version=__version__,
        description=(
            "kokoro-tone: an OpenAI-compatible speech gateway. `createSpeech` "
            "and `listModels` keep OpenAI's shape; the pronunciation resource "
            "and the tone controls are optional extensions (ADR-0018)."
        ),
        routes=app.routes,
    )
    for item in schema.get("paths", {}).values():
        for operation in item.values():
            if not isinstance(operation, dict):
                continue
            slug = operation.get("operationId")
            if not slug:
                continue
            operation["x-kokoro-tone-roles"] = _roles(str(slug))
            operation.get("responses", {}).pop("422", None)
            for response in operation.get("responses", {}).values():
                if not isinstance(response, dict):
                    continue
                declared = response.get("content", {}).get("application/json", {}).get("schema")
                # FastAPI merges its inferred `dict` schema with the declared
                # model; a `$ref` with sibling `type`/`title` describes two
                # different shapes, so keep the model alone.
                if isinstance(declared, dict) and "$ref" in declared:
                    response["content"]["application/json"]["schema"] = {"$ref": declared["$ref"]}

    # `createSpeech` answers with bytes unless `dry_run=1`, when it answers
    # with the prepared view. One status, two media types; the model is
    # declared by the route so the schema component exists here.
    speech = schema.get("paths", {}).get("/v1/audio/speech", {}).get("post", {})
    content = speech.get("responses", {}).get("200", {}).get("content", {})
    for media_type in ("audio/mpeg", "audio/wav", "audio/pcm"):
        content.setdefault(media_type, {"schema": {"type": "string", "format": "binary"}})
    return schema


class ContractApp(FastAPI):
    """A FastAPI app whose OpenAPI document is built by `build` and cached."""

    def openapi(self) -> dict[str, Any]:
        if self.openapi_schema is None:
            self.openapi_schema = build(self)
        return self.openapi_schema
