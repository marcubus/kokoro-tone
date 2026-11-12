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
import secrets
import time
from collections.abc import Callable, Iterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import __version__, audio, levelling, pipeline, pronounce
from .context import Context, get_context
from .log import event
from .request import MODELS, SpeechRequest
from .upstream import UpstreamError

router = APIRouter()
Ctx = Annotated[Context, Depends(get_context)]


def _guessed(found: list[pronounce.Ambiguity], mode: str) -> str | None:
    """The compact `word=sense` list, deduplicated in first-seen order."""
    if mode != "guess" or not found:
        return None
    assumed = list(dict.fromkeys(item.chosen for item in found))
    return ",".join(assumed)[:512]


def _mark_guessed(response: Response, found: list[pronounce.Ambiguity], mode: str) -> Response:
    """A guess never fails the render: it says what it assumed, in a header."""
    guessed = _guessed(found, mode)
    if guessed:
        response.headers["X-Kokoro-Tone-Guessed"] = guessed
    return response


def _layers(flags: pronounce.Flags) -> str:
    """The normalisation layers a request actually ran, for the log line."""
    on = [name for name in ("lexicon", "acronyms", "numbers") if getattr(flags, name)]
    return "+".join(on) or "none"


def require_auth(request: Request, ctx: Ctx) -> None:
    if not ctx.settings.gateway_keys:
        return
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    presented = presented.strip()
    matched = False
    if scheme.lower() == "bearer" and presented:
        # No early exit: response timing must not reveal which token matched.
        for key in ctx.settings.gateway_keys:
            matched |= secrets.compare_digest(presented, key)
    if not matched:
        raise HTTPException(
            status_code=401,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


Auth = Annotated[None, Depends(require_auth)]


@router.get("/health")
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
        "voice_levelling": {
            "enabled": ctx.settings.voice_levelling,
            "voices": len(levelling.VOICE_GAIN_DB),
        },
        "normalise": {
            "default": ctx.settings.normalise.default_on,
            "lexicon": ctx.settings.normalise.lexicon,
            "acronyms": ctx.settings.normalise.acronyms,
            "numbers": ctx.settings.normalise.numbers,
            "locale": ctx.settings.normalise.locale,
            "rules": len(data.defaults.rules),
            "voice_sets": len(data.voices),
            "server_aliases": len(data.aliases),
        },
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


@router.get("/v1/models")
def models(_: Auth) -> dict:
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "hexgrad"}
            for model in MODELS
        ],
    }


@router.get("/v1/audio/voices")
def voices(ctx: Ctx, _: Auth) -> object:
    try:
        return ctx.upstream.voices()
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.detail) from exc


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


@router.post("/v1/audio/speech")
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
