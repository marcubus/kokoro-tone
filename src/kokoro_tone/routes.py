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
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import __version__, audio, filters, levelling, pipeline, pronounce, script
from .access import PUBLIC_SLUGS, ROLE_ADMIN, Role
from .context import Context, get_context
from .log import event
from .parse import MarkupError
from .request import MODELS, SpeechRequest
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


class LookupRequest(BaseModel):
    words: list[Annotated[str, Field(max_length=200)]] = Field(min_length=1, max_length=100)


class LearnRequest(BaseModel):
    word: str = Field(min_length=1, max_length=200)
    say: str = Field(min_length=1, max_length=200)
    sense: str | None = Field(default=None, max_length=64)


class ForgetRequest(BaseModel):
    word: str = Field(min_length=1, max_length=200)
    sense: str | None = Field(default=None, max_length=64)


@router.get("/health", operation_id="getHealth")
def health(ctx: Ctx) -> JSONResponse:
    ok, detail = ctx.upstream.probe()
    data = ctx.lexicon.current()
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
            "voice_sets": len(data.voices),
            "server_aliases": len(data.aliases),
        },
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


@router.get("/v1/models", operation_id="listModels")
def models(_: Auth) -> dict:
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "hexgrad"}
            for model in MODELS
        ],
    }


@router.get("/v1/audio/voices", operation_id="listVoices")
def voices(ctx: Ctx, _: Auth) -> object:
    try:
        return ctx.upstream.voices()
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


@router.post("/v1/pronunciations/lookup", operation_id="listPronunciations")
def lookup_pronunciations(body: LookupRequest, ctx: Ctx, _: Auth) -> dict:
    try:
        return {"results": [ctx.lexicon.lookup(word) for word in body.words]}
    except MarkupError as exc:
        raise HTTPException(400, str(exc)) from exc


@router.post("/v1/pronunciations/learn", operation_id="createPronunciation")
def learn_pronunciation(body: LearnRequest, ctx: Ctx, _: Auth) -> dict:
    try:
        return ctx.lexicon.learn(body.word, body.say, body.sense)
    except MarkupError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"could not write learned lexicon: {exc}") from exc


@router.post("/v1/pronunciations/forget", operation_id="deletePronunciation")
def forget_pronunciation(body: ForgetRequest, ctx: Ctx, _: Auth) -> dict:
    try:
        return ctx.lexicon.forget(body.word, body.sense)
    except MarkupError as exc:
        raise HTTPException(400, str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(403, str(exc)) from exc
    except OSError as exc:
        raise HTTPException(500, f"could not write learned lexicon: {exc}") from exc


@router.post("/v1/script/compile", operation_id="compileScript")
def compile_only(body: SpeechRequest, ctx: Ctx, _: Auth) -> Response:
    """Resolve a request into spoken text without touching the engine.

    This runs the same `pipeline.prepare` the render runs, so the guesses and
    the resolved text are exactly what a render would produce, in milliseconds
    instead of minutes. A caller checks them before paying for synthesis; the
    CLI (`python -m kokoro_tone.script_cli`) is the same compile with no server.
    """
    prepared = pipeline.prepare(body, ctx)
    payload = prepared.as_dict()
    event(
        "compile",
        status=200,
        chars=prepared.chars,
        turns=len(prepared.turns),
        voice=prepared.voice,
        normalise=_layers(prepared.flags),
        guess=_guessed(prepared.found, prepared.ambiguity),
    )
    return _mark_guessed(JSONResponse(payload), prepared.found, prepared.ambiguity)


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


@router.post("/v1/audio/speech", operation_id="createSpeech")
def speech(body: SpeechRequest, request: Request, ctx: Ctx, _: Auth) -> Response:
    prepared = pipeline.prepare(body, ctx)
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
