"""The HTTP surface: OpenAI request shape in, mastered audio out.

All routes live on one `APIRouter`, so `main.py` is only app assembly. Request
shaping (turns, voices, aliases, markup) is in `request.py`; the SSE envelope
stays here because it is a response format; the engine client is in
`upstream.py`.
"""

from __future__ import annotations

import base64
import json
import math
import time
from collections.abc import Callable, Iterator, Sequence
from typing import Annotated, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__, audio, filters, levelling, pipeline, pronounce, proposals, script
from .access import PUBLIC_SLUGS, ROLE_ADMIN, Role
from .context import Context, get_context
from .lexicon import validate_say, validate_sense, validate_word
from .log import event, warning
from .openapi import (
    DeletedObject,
    Health,
    ModelList,
    PreparedView,
    PronunciationList,
    PronunciationRow,
    ProposalRow,
    error_responses,
)
from .parse import MarkupError
from .request import MODELS, SpeechRequest
from .settings import APPROVAL_REVIEW
from .upstream import UpstreamError

router = APIRouter()
Ctx = Annotated[Context, Depends(get_context)]


def _guessed(found: Sequence[pronounce.Ambiguity], mode: str) -> str | None:
    """The compact `word=sense` list, deduplicated in first-seen order."""
    assumed = script.guesses(tuple(found), mode)
    return ",".join(assumed)[:512] or None


def _mark_guessed(response: Response, found: Sequence[pronounce.Ambiguity], mode: str) -> Response:
    """A guess never fails the render: it says what it assumed, in a header."""
    guessed = _guessed(found, mode)
    if guessed:
        response.headers["X-Kokoro-Tone-Guessed"] = guessed
    return response


def _layers(flags: pronounce.Flags) -> str:
    """The normalisation layers a request actually ran, for the log line."""
    return script.layers(flags)


class InsufficientScope(HTTPException):
    """A recognised key on a slug its role may not call (ADR-0016)."""

    code = "insufficient_scope"

    def __init__(self) -> None:
        super().__init__(
            status_code=403,
            detail="insufficient scope",
            headers={"WWW-Authenticate": 'Bearer error="insufficient_scope"'},
        )


class StateConflict(HTTPException):
    """A 409 whose code names the state, not the permission (ADR-0017 rule 8)."""

    def __init__(self, code: str, detail: str) -> None:
        self.code = code
        super().__init__(status_code=409, detail=detail)


def _slug(request: Request) -> str:
    """The matched route's operation id, which is the authorisation slug.

    No id means no match: the empty slug is in no allowlist, so a default key
    fails closed rather than falling back to a function name.
    """
    route = request.scope.get("route")
    return str(getattr(route, "operation_id", None) or "")


def require_auth(request: Request, ctx: Ctx) -> Role:
    """Authenticate to a role, then authorise the slug. In that order.

    With no key configured everything is allowed, exactly as before: turning
    auth on is adding a key, not editing the policy. A public slug needs no
    credential, which keeps `/health` working for the container healthcheck.
    """
    access = ctx.access
    slug = _slug(request)
    if not access.enabled or slug in PUBLIC_SLUGS:
        return ROLE_ADMIN

    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    presented = presented.strip()
    role = None
    if scheme.lower() == "bearer" and presented:
        # No early exit in `Access.match`: timing must not reveal which token
        # matched, on either list.
        role = access.match(presented)
    if role is None:
        event(
            "auth",
            result="unauthenticated",
            path=request.url.path,
            slug=slug or "unmatched",
        )
        raise HTTPException(
            status_code=401,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )
    if not access.allows(role, slug):
        event(
            "auth",
            result="forbidden",
            path=request.url.path,
            slug=slug or "unmatched",
            role=role,
        )
        raise InsufficientScope()
    return role


Auth = Annotated[Role, Depends(require_auth)]


class CreatePronunciationRequest(BaseModel):
    word: str = Field(min_length=1, max_length=200)
    say: str = Field(min_length=1, max_length=200)
    sense: str | None = Field(default=None, max_length=64)
    # The sentence the audition renders with and without the proposal.
    context: str | None = Field(default=None, max_length=500)


class DecideRequest(BaseModel):
    status: Literal["approved", "rejected"]
    reason: str | None = Field(default=None, max_length=500)


def _store_error(exc: proposals.StoreError) -> HTTPException:
    return HTTPException(500, f"proposal store: {exc}")


@router.get(
    "/health",
    operation_id="getHealth",
    responses={
        200: {"model": Health},
        503: {"model": Health, "description": "The engine is not reachable."},
        **error_responses(500),
    },
)
def health(ctx: Ctx) -> JSONResponse:
    ok, detail = ctx.upstream.probe()
    data = ctx.lexicon.current()
    try:
        counts = ctx.proposals.counts()
    except proposals.StoreError:
        counts = {}
    payload = {
        "status": "ok" if ok else "degraded",
        "detail": detail,
        "version": __version__,
        "model": MODELS[0],
        "upstream": ctx.settings.upstream_base_url,
        "rubberband": audio.has_rubberband(),
        "filters": sorted(filters.registry()),
        "voice_levelling": {
            "enabled": ctx.settings.voice_levelling,
            "voices": len(levelling.VOICE_GAIN_DB),
        },
        "access": {
            "enabled": ctx.access.enabled,
            "policy": "file" if ctx.access.policy.source else "builtin",
            "admin_keys": bool(ctx.access.admin_keys),
            "default_keys": bool(ctx.access.default_keys),
        },
        "normalise": {
            "default": ctx.settings.normalise.default_on,
            "lexicon": ctx.settings.normalise.lexicon,
            "acronyms": ctx.settings.normalise.acronyms,
            "numbers": ctx.settings.normalise.numbers,
            "locale": ctx.settings.normalise.locale,
            "rules": len(data.defaults.rules),
            "learned_rules": ctx.lexicon.learned_rules,
            "write": ctx.settings.lexicon_write,
            "approval": ctx.settings.approval,
            "pending_proposals": counts.get(proposals.PENDING, 0),
            "voice_sets": len(data.voices),
            "server_aliases": len(data.aliases),
        },
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


@router.get(
    "/v1/models",
    operation_id="listModels",
    responses={200: {"model": ModelList}, **error_responses(401, 403, 500)},
)
def models(_: Auth) -> dict:
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "hexgrad"}
            for model in MODELS
        ],
    }


@router.get(
    "/v1/audio/voices",
    operation_id="listVoices",
    responses={
        200: {"description": "The engine's voice catalogue."},
        **error_responses(401, 403, 500, 502),
    },
)
def voices(ctx: Ctx, _: Auth) -> object:
    try:
        return ctx.upstream.voices()
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@router.get(
    "/v1/pronunciations",
    operation_id="listPronunciations",
    responses={200: {"model": PronunciationList}, **error_responses(400, 401, 403, 500)},
)
def list_pronunciations(
    ctx: Ctx,
    _: Auth,
    word: Annotated[list[str] | None, Query()] = None,
    status: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=proposals.MAX_PAGE)] = proposals.DEFAULT_PAGE,
    after: Annotated[str | None, Query()] = None,
) -> dict:
    """One resource, two readings: rules for a word, or rows from the queue.

    `?word=` (repeatable, at most 100) returns every deployment rule for those
    words across layers, each row naming its `source`, its `id` and the word's
    `senses`. A rule id is derived from layer + match + say, not stored.

    Without `?word=`, this lists proposals oldest first: the work queue. Passing
    `?status=pending` is the undecided view; with no status filter the decided
    rows are included too, because the decision history is part of the resource.
    `limit` is clamped to 50 by default and 200 at most, and `after` is an
    exclusive id cursor, so paging is stable while rows are added.

    Nothing is applied by reading this endpoint, and a pending row never expires
    or applies itself (ADR-0017 rejects approve-by-timeout). A queue that is
    simply never read stays pending forever - safe, but silent; `/health`
    publishes `pending_proposals` and nothing alerts on it.
    """
    if word:
        if len(word) > 100:
            raise HTTPException(400, "at most 100 words per request")
        data: list[dict[str, object]] = []
        for item in word:
            try:
                found = ctx.lexicon.lookup(item)
            except MarkupError as exc:
                raise HTTPException(400, str(exc)) from exc
            for entry in cast(list[dict[str, object]], found["rules"]):
                data.append(
                    {
                        "object": "pronunciation",
                        "status": "applied",
                        "senses": found["senses"],
                        **entry,
                    }
                )
        return {"object": "list", "data": data}
    if status is not None and status not in proposals.STATUSES:
        raise HTTPException(400, f"status must be one of {sorted(proposals.STATUSES)}")
    try:
        rows = ctx.proposals.list(status=status, limit=limit, after=after)
    except proposals.StoreError as exc:
        raise _store_error(exc) from exc
    return {
        "object": "list",
        "data": [{"object": "proposal", **row.as_dict()} for row in rows],
    }


@router.post(
    "/v1/pronunciations",
    operation_id="createPronunciation",
    status_code=201,
    responses={
        201: {"model": PronunciationRow},
        202: {"model": ProposalRow, "description": "Queued for an admin decision."},
        **error_responses(400, 401, 403, 413, 500),
    },
)
def create_pronunciation(body: CreatePronunciationRequest, ctx: Ctx, role: Auth) -> JSONResponse:
    """Propose a reading, or apply it. The status says which happened.

    `force` mode applies the rule to the learned overlay and answers `201`.
    `review` mode appends a pending row and answers `202`; the rule is not in
    effect and the engine keeps its own reading. The row is durable before the
    `202` is sent.

    An uppercase run in `say` is a `400` in review - a stress guess is the thing
    under review - and a logged warning in force. The review queue has a row
    cap: when the *pending* rows alone would exceed `MAX_PROPOSALS`, this answers
    `400` rather than discarding undecided work. Decided rows are trimmed to make
    room first.
    """
    limits = ctx.settings.limits
    try:
        word = validate_word(body.word, limits)
        say = validate_say(body.say, limits)
        sense = validate_sense(body.sense) if body.sense else None
    except MarkupError as exc:
        raise HTTPException(400, str(exc)) from exc

    if proposals.has_uppercase_run(say):
        if ctx.settings.approval == APPROVAL_REVIEW:
            # An uppercase run is a proposer's stress guess, and in review the
            # guess is the thing under review, not a thing to queue.
            raise HTTPException(
                400,
                "say must be lowercase: an uppercase run is a stress guess, not a respelling",
            )
        warning("pronunciation: uppercase stress markers in a force-mode say")

    if ctx.settings.approval == APPROVAL_REVIEW:
        try:
            proposal = ctx.proposals.propose(
                word=word, say=say, sense=sense, context=body.context, role=role
            )
        except MarkupError as exc:
            raise HTTPException(400, str(exc)) from exc
        except proposals.StoreError as exc:
            raise _store_error(exc) from exc
        return JSONResponse({"object": "proposal", **proposal.as_dict()}, status_code=202)

    try:
        applied = ctx.lexicon.learn(word, say, sense)
    except MarkupError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"could not write learned lexicon: {exc}") from exc
    return JSONResponse(
        {"object": "pronunciation", "status": "applied", **applied}, status_code=201
    )


@router.patch(
    "/v1/pronunciations/{pron_id}",
    operation_id="updatePronunciation",
    responses={
        200: {"model": ProposalRow},
        **error_responses(400, 401, 403, 404, 409, 500),
    },
)
def update_pronunciation(pron_id: str, body: DecideRequest, ctx: Ctx, role: Auth) -> dict:
    """Approve or reject one proposal. Admin only, and only in review mode.

    An approval writes the rule to the learned overlay, then records the
    decision; those are two writes, not a transaction, so a crash between them
    leaves an applied rule whose row is still pending. Re-approving is safe
    because the overlay replaces by `match`. A decided row is immutable:
    a second decision, or a decision in `force` mode, is a `409`, and an id that
    names a shipped or user rule rather than a proposal is `409 immutable_rule`.
    """
    if ctx.settings.approval != APPROVAL_REVIEW:
        raise StateConflict("not_pending", "approval mode is force: there is nothing pending")
    try:
        proposal = ctx.proposals.get(pron_id)
        known_rule = None if proposal is not None else ctx.lexicon.rule_by_id(pron_id)
    except proposals.StoreError as exc:
        raise _store_error(exc) from exc
    if proposal is None:
        if known_rule is not None:
            raise StateConflict("immutable_rule", "this id names a rule, not a proposal")
        raise HTTPException(404, "proposal not found")
    if proposal.status != proposals.PENDING:
        raise StateConflict("invalid_transition", f"proposal is already {proposal.status}")

    applied = False
    if body.status == proposals.APPROVED:
        try:
            ctx.lexicon.apply_approved(proposal.word, proposal.say, proposal.sense)
        except MarkupError as exc:
            raise HTTPException(400, str(exc)) from exc
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except OSError as exc:
            raise HTTPException(500, f"could not write learned lexicon: {exc}") from exc
        applied = True

    try:
        updated = ctx.proposals.decide(
            pron_id, status=body.status, role=role, reason=body.reason, applied=applied
        )
    except proposals.StoreConflict as exc:
        raise StateConflict("invalid_transition", str(exc)) from exc
    except proposals.StoreError as exc:
        raise _store_error(exc) from exc
    return {"object": "proposal", **updated.as_dict()}


@router.delete(
    "/v1/pronunciations/{pron_id}",
    operation_id="deletePronunciation",
    responses={
        200: {"model": DeletedObject},
        **error_responses(400, 401, 403, 404, 409, 500),
    },
)
def delete_pronunciation(pron_id: str, ctx: Ctx, _: Auth) -> dict:
    """Remove a proposal row, or delete one learned rule by id. Admin only.

    The two cases are separate on purpose. Deleting a proposal removes the row
    and its history; it does **not** remove a rule an earlier approval applied -
    that rule lives in `learned.json` and is deleted by its own `learned-…` id
    from `GET /v1/pronunciations?word=`. Deleting a shipped default or a
    `custom.json` rule is `409 immutable_rule`; an unknown id is `404`.
    """
    try:
        if ctx.proposals.delete(pron_id):
            return {"object": "proposal", "id": pron_id, "deleted": True}
    except proposals.StoreError as exc:
        raise _store_error(exc) from exc
    try:
        outcome = ctx.lexicon.delete_rule(pron_id)
    except MarkupError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"could not write learned lexicon: {exc}") from exc
    if outcome is None:
        raise HTTPException(404, "pronunciation not found")
    if outcome == "immutable":
        raise StateConflict("immutable_rule", "only a learned rule may be deleted")
    return {"object": "pronunciation", "id": pron_id, "deleted": True}


@router.get(
    "/v1/pronunciations/{pron_id}/audio",
    operation_id="retrievePronunciationAudio",
    responses={
        200: {
            "description": "One take of the proposal's context sentence.",
            "content": {
                "audio/mpeg": {"schema": {"type": "string", "format": "binary"}},
                "audio/wav": {"schema": {"type": "string", "format": "binary"}},
                "audio/pcm": {"schema": {"type": "string", "format": "binary"}},
            },
        },
        **error_responses(400, 401, 403, 404, 500, 502),
    },
)
def pronunciation_audio(
    pron_id: str,
    ctx: Ctx,
    _: Auth,
    variant: Annotated[str, Query()] = "proposed",
    response_format: Annotated[str, Query()] = "mp3",
) -> Response:
    """The audition: the proposal's own sentence with and without the reading.

    Approving from spelling is what produced the uppercase markers, so the
    decision is made on sound: two takes of the same sentence, one difference.
    `as-is` renders the context with no rule; `proposed` applies the proposal's
    bare match (a sense marker would not fire in a plain sentence). A proposal
    with no context renders the word alone. Both variants are available after a
    decision as well, and rendering here has no side effect on the row.
    """
    if variant not in ("as-is", "proposed"):
        raise HTTPException(400, "variant must be 'as-is' or 'proposed'")
    try:
        proposal = ctx.proposals.get(pron_id)
    except proposals.StoreError as exc:
        raise _store_error(exc) from exc
    if proposal is None:
        raise HTTPException(404, "proposal not found")

    rules = None
    if variant == "proposed":
        # The audition text is a plain sentence, so the bare word is the match
        # even when the proposal carries a sense: a marker would not fire.
        rules = [{"match": proposal.word, "say": proposal.say}]
    body = SpeechRequest(
        input=proposal.context or proposal.word,
        voice=ctx.settings.default_voice,
        pronunciations=rules,
        response_format=response_format,
    )
    prepared = pipeline.prepare(body, ctx)
    try:
        rendered = pipeline.render(prepared, body, ctx)
    except audio.AudioError as exc:
        raise HTTPException(502, f"audio processing failed: {exc}") from exc
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    event(
        "pronunciation_audition",
        variant=variant,
        chars=prepared.chars,
        bytes=len(rendered.data),
    )
    return Response(rendered.data, media_type=rendered.media_type)


@router.post(
    "/v1/audio/speech",
    operation_id="createSpeech",
    responses={
        200: {
            "model": PreparedView,
            "description": (
                "The rendered audio, or with `dry_run=1` the render's own "
                "prepared view as JSON and no audio."
            ),
            "headers": {
                "X-Kokoro-Tone-Guessed": {
                    "description": "The compact `word=sense` list the rules assumed.",
                    "schema": {"type": "string"},
                }
            },
        },
        **error_responses(400, 401, 403, 413, 500, 502),
    },
)
def speech(
    body: SpeechRequest,
    request: Request,
    ctx: Ctx,
    _: Auth,
    dry_run: Annotated[bool, Query()] = False,
) -> Response:
    """Render speech, or with `dry_run=1` return the prepared view.

    The dry run shares this handler's `pipeline.prepare`, so its `text`,
    `guesses` and `ambiguities` are the render's own; only the engine call and
    the audio are skipped (ADR-0018 rule 7).
    """
    prepared = pipeline.prepare(body, ctx)
    if dry_run:
        # The dry run is the render's own preparation, not a second entry
        # point: same body, same guesses, no engine (ADR-0018 rule 7).
        event(
            "compile",
            status=200,
            chars=prepared.chars,
            turns=len(prepared.turns),
            voice=prepared.voice,
            normalise=_layers(prepared.flags),
            guess=_guessed(prepared.found, prepared.ambiguity),
        )
        return _mark_guessed(JSONResponse(prepared.as_dict()), prepared.found, prepared.ambiguity)
    started = time.perf_counter()
    spoken = {"chars": prepared.chars, "turns": len(prepared.turns)}

    if prepared.passthrough:
        assert isinstance(prepared.payload, str)
        response = _passthrough(
            body,
            prepared.voice,
            prepared.payload,
            ctx,
            volume_multiplier=pipeline.volume_multiplier(prepared.voice, ctx),
            sse=prepared.stream_format == "sse",
        )
        request.state.streaming = True
        _record_speech(
            ctx,
            passthrough=True,
            stream_format=prepared.stream_format,
            fmt=body.response_format,
            chars=prepared.chars,
        )
        event(
            "speech",
            status=200,
            passthrough="yes",
            stream=prepared.stream_format,
            **spoken,
            voice=prepared.voice,
            fmt=body.response_format,
            normalise=_layers(prepared.flags),
            guess=_guessed(prepared.found, prepared.ambiguity),
            ms=round((time.perf_counter() - started) * 1000.0),
        )
        return _mark_guessed(response, prepared.found, prepared.ambiguity)

    try:
        rendered = pipeline.render(prepared, body, ctx)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except audio.AudioError as exc:
        raise HTTPException(502, f"audio processing failed: {exc}") from exc
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.detail) from exc

    _record_speech(
        ctx,
        passthrough=False,
        stream_format=prepared.stream_format,
        fmt=body.response_format,
        chars=prepared.chars,
    )
    total_ms = (time.perf_counter() - started) * 1000.0
    event(
        "speech",
        status=200,
        passthrough="no",
        stream=prepared.stream_format,
        **spoken,
        voice=prepared.voice,
        fmt=body.response_format,
        normalise=_layers(prepared.flags),
        guess=_guessed(prepared.found, prepared.ambiguity),
        upstream_ms=round(rendered.upstream_ms),
        render_ms=round(total_ms - rendered.upstream_ms),
        bytes=len(rendered.data),
    )

    if prepared.stream_format == "sse":
        # Mastered output cannot be emitted before the file is complete, so the
        # events arrive after rendering rather than during it.
        request.state.streaming = True
        return _mark_guessed(
            _sse_response(_chunks(rendered.data), _usage(prepared.payload)),
            prepared.found,
            prepared.ambiguity,
        )

    return _mark_guessed(
        Response(content=rendered.data, media_type=rendered.media_type),
        prepared.found,
        prepared.ambiguity,
    )


def _record_speech(
    ctx: Context,
    *,
    passthrough: bool,
    stream_format: str,
    fmt: str,
    chars: int,
) -> None:
    attributes: dict[str, str | bool] = {
        "kokoro_tone.passthrough": passthrough,
        "kokoro_tone.stream_format": stream_format,
        "kokoro_tone.format": fmt.lower(),
    }
    ctx.telemetry.speech_requests.add(1, attributes)
    ctx.telemetry.speech_chars.record(chars, {"kokoro_tone.passthrough": passthrough})


def _passthrough(
    body: SpeechRequest,
    voice: str,
    text: str,
    ctx: Context,
    *,
    volume_multiplier: float | None = None,
    sse: bool = False,
) -> Response:
    # SSE needs the engine to emit incrementally; otherwise honour `stream`.
    try:
        upstream = ctx.upstream.open_stream(
            text,
            voice,
            body.response_format,
            body.speed,
            True if sse else bool(body.stream),
            volume_multiplier,
        )
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.detail) from exc

    if sse:
        return _sse_response(upstream.iter_raw(), _usage(text), on_close=upstream.close)

    media_type = upstream.headers.get("content-type", "audio/mpeg").split(";")[0]

    def body_stream() -> Iterator[bytes]:
        try:
            yield from upstream.iter_raw()
        finally:
            upstream.close()

    return StreamingResponse(
        body_stream(),
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


def _chunks(data: bytes, size: int = 65536) -> Iterator[bytes]:
    for start in range(0, len(data), size):
        yield data[start : start + size]


def _usage(payload: object) -> dict[str, int]:
    """Token counts are estimated from characters; Kokoro reports none."""
    text = payload if isinstance(payload, str) else str(payload)
    tokens = max(1, math.ceil(len(text) / 4))
    return {"input_tokens": tokens, "output_tokens": 0, "total_tokens": tokens}


def _sse_event(event: str, payload: dict) -> bytes:
    body = json.dumps(payload, separators=(",", ":"))
    return f"event: {event}\ndata: {body}\n\n".encode()


def _sse_response(
    chunks: Iterator[bytes],
    usage: dict[str, int],
    on_close: Callable[[], None] | None = None,
) -> StreamingResponse:
    """OpenAI speech SSE: base64 `speech.audio.delta` frames, then `.done`."""

    def generate() -> Iterator[bytes]:
        try:
            for chunk in chunks:
                if not chunk:
                    continue
                yield _sse_event(
                    "speech.audio.delta",
                    {
                        "type": "speech.audio.delta",
                        "audio": base64.b64encode(chunk).decode("ascii"),
                    },
                )
        finally:
            if on_close is not None:
                on_close()
        yield _sse_event("speech.audio.done", {"type": "speech.audio.done", "usage": usage})

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
    )
