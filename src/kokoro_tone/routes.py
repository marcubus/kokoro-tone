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

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from . import (
    __version__,
    audio,
    levelling,
    lexicon,
    parse,
    pronounce,
    prosody,
    request,
    settings,
)
from .log import event
from .request import MODELS, SpeechRequest
from .upstream import (
    UpstreamError,
    list_voices,
    open_stream,
    probe,
    synthesise,
)

VOICES_TTL_SECONDS = 300

LEXICON = lexicon.Lexicon(
    settings.SETTINGS.lexicon_path,
    settings.SETTINGS.limits,
    settings.SETTINGS.lexicon_overlay_path,
)

router = APIRouter()


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


def _volume_multiplier(voice: str | None) -> float | None:
    """The static casting gain for a voice, or None when it is unity/disabled."""
    if not settings.SETTINGS.voice_levelling:
        return None
    value = levelling.multiplier(voice or settings.SETTINGS.default_voice)
    return value if abs(value - 1.0) > 1e-4 else None


def require_auth(request: Request) -> None:
    if not settings.SETTINGS.gateway_keys:
        return
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    presented = presented.strip()
    matched = False
    if scheme.lower() == "bearer" and presented:
        # No early exit: response timing must not reveal which token matched.
        for key in settings.SETTINGS.gateway_keys:
            matched |= secrets.compare_digest(presented, key)
    if not matched:
        raise HTTPException(
            status_code=401,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get("/health")
def health() -> JSONResponse:
    ok, detail = probe()
    data = LEXICON.current()
    payload = {
        "status": "ok" if ok else "degraded",
        "detail": detail,
        "version": __version__,
        "model": MODELS[0],
        "upstream": settings.SETTINGS.upstream_base_url,
        "rubberband": audio.has_rubberband(),
        "voice_levelling": {
            "enabled": settings.SETTINGS.voice_levelling,
            "voices": len(levelling.VOICE_GAIN_DB),
        },
        "normalise": {
            "default": settings.SETTINGS.normalise.default_on,
            "lexicon": settings.SETTINGS.normalise.lexicon,
            "acronyms": settings.SETTINGS.normalise.acronyms,
            "numbers": settings.SETTINGS.normalise.numbers,
            "locale": settings.SETTINGS.normalise.locale,
            "rules": len(data.defaults.rules),
            "voice_sets": len(data.voices),
            "server_aliases": len(data.aliases),
        },
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


@router.get("/v1/models")
def models(_: None = Depends(require_auth)) -> dict:
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "hexgrad"}
            for model in MODELS
        ],
    }


_voices_cache: dict[str, object] = {"at": 0.0, "data": None}


@router.get("/v1/audio/voices")
def voices(_: None = Depends(require_auth)) -> object:
    now = time.monotonic()
    cached = _voices_cache["data"]
    if cached is not None and now - float(_voices_cache["at"]) < VOICES_TTL_SECONDS:
        return cached
    try:
        _voices_cache["data"] = list_voices()
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.detail) from exc
    _voices_cache["at"] = now
    return _voices_cache["data"]


@router.post("/v1/audio/speech")
def speech(body: SpeechRequest, _: None = Depends(require_auth)) -> Response:
    limits = settings.SETTINGS.limits
    request.check_model(body.model)

    if body.speed is not None and not (limits.speed_min <= body.speed <= limits.speed_max):
        raise HTTPException(400, f"speed must be between {limits.speed_min} and {limits.speed_max}")
    if (body.response_format or "mp3").lower() not in audio.FORMATS:
        known = ", ".join(sorted(audio.FORMATS))
        raise HTTPException(400, f"unsupported response_format; use {known}")
    stream_format = (body.stream_format or "audio").lower()
    if stream_format not in ("audio", "sse"):
        raise HTTPException(400, "stream_format must be 'audio' or 'sse'")
    ambiguity = (body.ambiguity or "guess").strip().lower()
    if ambiguity not in ("guess", "ignore"):
        raise HTTPException(400, "ambiguity must be 'guess' or 'ignore'")

    instructions = request.clean_instructions(body.instructions, limits)
    data = LEXICON.current()
    aliases, voice = request.voice_spec(body.voice, limits, data)
    default_voice = voice or settings.SETTINGS.default_voice
    payload = request.sanitise_input(body.input)

    try:
        flags = pronounce.parse_flags(body.normalise, settings.SETTINGS.normalise)
        request_global, request_alias = lexicon.parse_request_sets(body.pronunciations, limits)
    except parse.MarkupError as exc:
        raise HTTPException(400, str(exc)) from exc

    token = request.voice_token(body.voice)
    found: list[pronounce.Ambiguity] = []
    collect = found if ambiguity != "ignore" else None
    guessing = ambiguity == "guess"
    plain = isinstance(payload, str) and not parse.has_markup(payload)
    if plain:
        payload = request.rewrite(
            payload,
            token,
            default_voice,
            request_global,
            request_alias,
            data,
            flags,
            settings.SETTINGS.normalise,
            limits,
            collect,
            guessing,
        )
        if len(payload) > limits.input_chars:
            raise HTTPException(400, f"input exceeds {limits.input_chars} characters")

    try:
        turns = parse.parse_input(payload, default_voice, limits)
    except parse.MarkupError as exc:
        raise HTTPException(400, str(exc)) from exc

    tokens = [turn.voice for turn in turns]
    request.apply_aliases(turns, aliases)

    if not plain:
        for turn, turn_token in zip(turns, tokens, strict=False):
            turn.text = request.rewrite(
                turn.text,
                turn_token,
                turn.voice,
                request_global,
                request_alias,
                data,
                flags,
                settings.SETTINGS.normalise,
                limits,
                collect,
                guessing,
                turn.markers,
            )
        total = sum(len(turn.text) for turn in turns)
        if total > limits.input_chars:
            raise HTTPException(400, f"input exceeds {limits.input_chars} characters")

    # Every field below is a count, a name or a duration. The text itself never
    # reaches the log; it is the caller's script, not the operator's business.
    started = time.perf_counter()
    upstream_ms = 0.0
    spoken = {"chars": sum(len(turn.text) for turn in turns), "turns": len(turns)}

    def render_turn(text: str, voice: str | None) -> bytes:
        nonlocal upstream_ms
        at = time.perf_counter()
        try:
            return synthesise(
                text,
                voice,
                volume_multiplier=_volume_multiplier(voice or default_voice),
            )
        finally:
            upstream_ms += (time.perf_counter() - at) * 1000.0

    if request.is_passthrough(payload, instructions):
        response = _passthrough(
            body,
            default_voice,
            payload,
            volume_multiplier=_volume_multiplier(default_voice),
            sse=stream_format == "sse",
        )
        event(
            "speech",
            status=200,
            passthrough="yes",
            stream=stream_format,
            **spoken,
            voice=default_voice,
            fmt=body.response_format,
            normalise=_layers(flags),
            guess=_guessed(found, ambiguity),
            ms=round((time.perf_counter() - started) * 1000.0),
        )
        return _mark_guessed(response, found, ambiguity)

    turns = parse.split_long(turns, limits.segment_chars)

    try:
        pcm = audio.render(
            turns,
            render_turn,
            speed=body.speed or 1.0,
            instructions=instructions,
            target_lufs=settings.SETTINGS.loudness_lufs,
            limits=limits,
        )
        data, media_type = audio.encode(pcm, body.response_format)
    except prosody.UnknownEmotion as exc:
        raise HTTPException(400, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except audio.AudioError as exc:
        raise HTTPException(502, f"audio processing failed: {exc}") from exc
    except UpstreamError as exc:
        raise HTTPException(exc.status, exc.detail) from exc

    total_ms = (time.perf_counter() - started) * 1000.0
    event(
        "speech",
        status=200,
        passthrough="no",
        stream=stream_format,
        **spoken,
        voice=default_voice,
        fmt=body.response_format,
        normalise=_layers(flags),
        guess=_guessed(found, ambiguity),
        upstream_ms=round(upstream_ms),
        render_ms=round(total_ms - upstream_ms),
        bytes=len(data),
    )

    if stream_format == "sse":
        # Mastered output cannot be emitted before the file is complete, so the
        # events arrive after rendering rather than during it.
        return _mark_guessed(_sse_response(_chunks(data), _usage(payload)), found, ambiguity)

    return _mark_guessed(Response(content=data, media_type=media_type), found, ambiguity)


def _passthrough(
    body: SpeechRequest,
    voice: str,
    text: str,
    *,
    volume_multiplier: float | None = None,
    sse: bool = False,
) -> Response:
    # SSE needs the engine to emit incrementally; otherwise honour `stream`.
    try:
        upstream = open_stream(
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

    def body_stream():
        try:
            yield from upstream.iter_raw()
        finally:
            upstream.close()

    return StreamingResponse(
        body_stream(),
        media_type=media_type,
        headers={"Cache-Control": "no-store"},
    )


def _chunks(data: bytes, size: int = 65536):
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


def _sse_response(chunks, usage: dict[str, int], on_close=None) -> StreamingResponse:
    """OpenAI speech SSE: base64 `speech.audio.delta` frames, then `.done`."""

    def generate():
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
