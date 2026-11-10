"""FastAPI gateway: OpenAI request shape in, mastered Kokoro audio out."""

from __future__ import annotations

import asyncio
import base64
import json
import math
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel

from . import __version__, audio, parse, prosody
from .limits import Limits, load_limits

# The only model this service serves. `kokoro`, `tts-1` and `tts-1-hd` are
# accepted as wire-compatible aliases; `gpt-4o-mini-tts` is rejected because it
# names OpenAI's instruction-following model, which this is not.
MODELS = ("kokoro-v1_0",)
ACCEPTED_MODELS = frozenset({"kokoro-v1_0", "kokoro", "tts-1", "tts-1-hd"})
REJECTED_MODELS = frozenset({"gpt-4o-mini-tts"})

VOICES_TTL_SECONDS = 300

# -16 LUFS is the usual podcast deliverable. The lower bound leaves room for
# ACX-style audiobooks (-20) and quiet narration; the upper bound keeps enough
# peak headroom that the +4 dB `angry` preset is not driven into the -1 dBTP
# limiter, which would flatten the emotion presets.
PODCAST_LUFS = -16.0
LOUDNESS_MIN = -30.0
LOUDNESS_MAX = -12.0


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str
    upstream_api_key: str
    upstream_model: str
    gateway_keys: tuple[str, ...]
    default_voice: str
    loudness_lufs: float
    limits: Limits


def _token_list(*values: str) -> tuple[str, ...]:
    """Merge comma-separated token lists, dropping blanks and duplicates."""
    tokens: list[str] = []
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if token and token not in tokens:
                tokens.append(token)
    return tuple(tokens)


def load_settings() -> Settings:
    def loudness() -> float:
        raw = os.environ.get("LOUDNESS_LUFS", "")
        try:
            target = float(raw) if raw.strip() else PODCAST_LUFS
        except ValueError:
            target = PODCAST_LUFS
        if not math.isfinite(target):
            target = PODCAST_LUFS
        clamped = max(LOUDNESS_MIN, min(LOUDNESS_MAX, target))
        if clamped != target:
            print(
                f"LOUDNESS_LUFS {target} is outside {LOUDNESS_MIN}..{LOUDNESS_MAX}; "
                f"using {clamped}",
                file=sys.stderr,
            )
        return clamped

    return Settings(
        upstream_base_url=os.environ.get("UPSTREAM_BASE_URL", "http://kokoro:8880/v1").rstrip("/"),
        # One secret in .env: an explicit UPSTREAM_API_KEY wins (OpenRouter
        # fallback), otherwise the engine key is reused.
        upstream_api_key=os.environ.get("UPSTREAM_API_KEY")
        or os.environ.get("KOKORO_FASTAPI_API_KEY", ""),
        upstream_model=os.environ.get("UPSTREAM_MODEL", "kokoro"),
        gateway_keys=_token_list(
            os.environ.get("KOKORO_TONE_API_KEYS", ""),
            os.environ.get("KOKORO_TONE_API_KEY", ""),
        ),
        default_voice=os.environ.get("DEFAULT_VOICE", "af_heart"),
        loudness_lufs=loudness(),
        limits=load_limits(),
    )


SETTINGS = load_settings()


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


class SpeechRequest(BaseModel):
    model: str = "kokoro-v1_0"
    input: object
    # A voice name, or a map of alias -> voice for this request. An `id` entry
    # (string or {"id": ...}) sets the default for untagged text.
    voice: str | dict[str, object] | None = None
    instructions: str | None = None
    response_format: str = "mp3"
    speed: float = 1.0
    # OpenAI's field. `audio` (default) is a binary body; `sse` wraps the same
    # bytes in speech.audio.delta events.
    stream_format: str | None = None
    # Kokoro/OpenRouter's field, kept as an alias for raw audio streaming.
    stream: bool = False


def _upstream_headers() -> dict[str, str]:
    if SETTINGS.upstream_api_key:
        return {"Authorization": f"Bearer {SETTINGS.upstream_api_key}"}
    return {}


def _upstream_url(path: str) -> str:
    return f"{SETTINGS.upstream_base_url}/{path.lstrip('/')}"


def _raise_for_upstream(response: httpx.Response) -> None:
    if response.status_code < 400:
        return
    if response.status_code == 400:
        raise UpstreamError(400, _detail(response))
    if response.status_code in (401, 403):
        raise UpstreamError(502, "upstream rejected the gateway credentials")
    raise UpstreamError(502, f"upstream returned {response.status_code}: {_detail(response)}")


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:400] or "no detail"
    if isinstance(body, dict):
        for key in ("detail", "error", "message"):
            if key in body:
                return str(body[key])[:400]
    return str(body)[:400]


def _synthesize(text: str, voice: str | None) -> bytes:
    payload = {
        "model": SETTINGS.upstream_model,
        "input": text,
        "voice": voice or SETTINGS.default_voice,
        "response_format": "pcm",
        "stream": False,
    }
    try:
        response = http_client().post(
            _upstream_url("audio/speech"),
            json=payload,
            headers=_upstream_headers(),
        )
    except httpx.HTTPError as exc:
        raise UpstreamError(502, f"upstream unreachable: {exc}") from exc
    _raise_for_upstream(response)
    return response.content


_client: httpx.Client | None = None


def http_client() -> httpx.Client:
    global _client
    if _client is None:
        _client = httpx.Client(timeout=httpx.Timeout(180.0, connect=10.0))
    return _client


@asynccontextmanager
async def lifespan(app: FastAPI):
    http_client()
    yield
    global _client
    if _client is not None:
        _client.close()
        _client = None


app = FastAPI(title="kokoro-tone", version=__version__, lifespan=lifespan)

_ERROR_TYPES = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "invalid_request_error",
    413: "invalid_request_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
}


def _error_body(
    message: str, status: int, param: str | None = None, code: str | None = None
) -> dict:
    """OpenAI's error envelope, so SDK error handling behaves."""
    if status >= 500:
        error_type = "server_error"
    else:
        error_type = _ERROR_TYPES.get(status, "api_error")
    if status == 401:
        code = code or "invalid_api_key"
    return {
        "error": {
            "message": message,
            "type": error_type,
            "param": param,
            "code": code,
        }
    }


class BodyLimitMiddleware:
    """Reject oversized bodies before FastAPI parses them.

    The body is buffered so the limit also holds for chunked requests without a
    content-length. Replay blocks after the body is delivered, which is what a
    real `receive` does, so streaming responses keep working.
    """

    def __init__(self, app, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        declared = None
        for key, value in scope.get("headers", []):
            if key == b"content-length":
                try:
                    declared = int(value)
                except ValueError:
                    declared = None
        if declared is not None and declared > self.max_bytes:
            await self._reject(send)
            return

        body = bytearray()
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body.extend(message.get("body", b""))
            if len(body) > self.max_bytes:
                await self._reject(send)
                return
            if not message.get("more_body", False):
                break

        delivered = False

        async def replay():
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": bytes(body), "more_body": False}
            await asyncio.Event().wait()

        await self.app(scope, replay, send)

    async def _reject(self, send) -> None:
        payload = json.dumps(
            _error_body(
                f"request body exceeds {self.max_bytes} bytes",
                413,
                code="body_too_large",
            )
        ).encode()
        await send(
            {
                "type": "http.response.start",
                "status": 413,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode()),
                ],
            }
        )
        await send({"type": "http.response.body", "body": payload})


app.add_middleware(BodyLimitMiddleware, max_bytes=SETTINGS.limits.body_bytes)


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, exc: HTTPException) -> JSONResponse:
    return JSONResponse(
        _error_body(str(exc.detail), exc.status_code),
        status_code=exc.status_code,
        headers=exc.headers,
    )


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    errors = exc.errors()
    param = None
    message = "invalid request"
    if errors:
        first = errors[0]
        location = [str(part) for part in first.get("loc", ()) if part != "body"]
        param = ".".join(location) or None
        message = str(first.get("msg", message))
    return JSONResponse(
        _error_body(message, 400, param=param, code="invalid_value"),
        status_code=400,
    )


@app.exception_handler(Exception)
async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(_error_body("internal server error", 500), status_code=500)


def require_auth(request: Request) -> None:
    if not SETTINGS.gateway_keys:
        return
    scheme, _, presented = request.headers.get("authorization", "").partition(" ")
    presented = presented.strip()
    matched = False
    if scheme.lower() == "bearer" and presented:
        # No early exit: response timing must not reveal which token matched.
        for key in SETTINGS.gateway_keys:
            matched |= secrets.compare_digest(presented, key)
    if not matched:
        raise HTTPException(
            status_code=401,
            detail="invalid or missing bearer token",
            headers={"WWW-Authenticate": "Bearer"},
        )


@app.get("/health")
def health() -> JSONResponse:
    ok = False
    detail = "upstream unreachable"
    try:
        response = http_client().get(
            _upstream_url("health"), headers=_upstream_headers(), timeout=5.0
        )
        ok = response.status_code == 200
        detail = "ok" if ok else f"upstream returned {response.status_code}"
    except httpx.HTTPError as exc:
        detail = f"upstream unreachable: {exc.__class__.__name__}"
    payload = {
        "status": "ok" if ok else "degraded",
        "detail": detail,
        "version": __version__,
        "model": MODELS[0],
        "upstream": SETTINGS.upstream_base_url,
        "rubberband": audio.has_rubberband(),
    }
    return JSONResponse(payload, status_code=200 if ok else 503)


@app.get("/v1/models")
def models(_: None = Depends(require_auth)) -> dict:
    return {
        "object": "list",
        "data": [
            {"id": model, "object": "model", "created": 0, "owned_by": "hexgrad"}
            for model in MODELS
        ],
    }


_voices_cache: dict[str, object] = {"at": 0.0, "data": None}


@app.get("/v1/audio/voices")
def voices(_: None = Depends(require_auth)) -> object:
    now = time.monotonic()
    cached = _voices_cache["data"]
    if cached is not None and now - float(_voices_cache["at"]) < VOICES_TTL_SECONDS:
        return cached
    try:
        response = http_client().get(
            _upstream_url("audio/voices"), headers=_upstream_headers(), timeout=15.0
        )
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"upstream unreachable: {exc.__class__.__name__}") from exc
    if response.status_code != 200:
        raise HTTPException(502, f"upstream returned {response.status_code}")
    _voices_cache["data"] = response.json()
    _voices_cache["at"] = now
    return _voices_cache["data"]


@app.post("/v1/audio/speech")
def speech(body: SpeechRequest, _: None = Depends(require_auth)) -> Response:
    limits = SETTINGS.limits
    _check_model(body.model)

    if body.speed is not None and not (limits.speed_min <= body.speed <= limits.speed_max):
        raise HTTPException(400, f"speed must be between {limits.speed_min} and {limits.speed_max}")
    if (body.response_format or "mp3").lower() not in audio.FORMATS:
        known = ", ".join(sorted(audio.FORMATS))
        raise HTTPException(400, f"unsupported response_format; use {known}")
    stream_format = (body.stream_format or "audio").lower()
    if stream_format not in ("audio", "sse"):
        raise HTTPException(400, "stream_format must be 'audio' or 'sse'")

    instructions = _instructions(body.instructions, limits)
    aliases, voice = _voice_spec(body.voice, limits)
    default_voice = voice or SETTINGS.default_voice
    payload = _sanitized_input(body.input)

    try:
        turns = parse.parse_input(payload, default_voice, limits)
    except parse.MarkupError as exc:
        raise HTTPException(400, str(exc)) from exc

    _apply_aliases(turns, aliases)

    if _is_passthrough(payload, instructions):
        return _passthrough(body, default_voice, payload, sse=stream_format == "sse")

    turns = parse.split_long(turns, limits.segment_chars)

    try:
        pcm = audio.render(
            turns,
            _synthesize,
            speed=body.speed or 1.0,
            instructions=instructions,
            target_lufs=SETTINGS.loudness_lufs,
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

    if stream_format == "sse":
        # Mastered output cannot be emitted before the file is complete, so the
        # events arrive after rendering rather than during it.
        return _sse_response(_chunks(data), _usage(payload))

    return Response(content=data, media_type=media_type)


def _check_model(model: str | None) -> None:
    name = (model or MODELS[0]).strip()
    if name in REJECTED_MODELS:
        raise HTTPException(
            400,
            "this gateway serves Kokoro only; GPT TTS models are not available",
        )
    if name not in ACCEPTED_MODELS:
        known = ", ".join(sorted(ACCEPTED_MODELS))
        raise HTTPException(400, f"unknown model; use one of {known}")


def _instructions(value: str | None, limits: Limits) -> str | None:
    if not value:
        return None
    text = parse.sanitize_text(value)
    if len(text) > limits.instructions_chars:
        raise HTTPException(400, f"instructions exceeds {limits.instructions_chars} characters")
    return text


def _sanitized_input(payload: object) -> object:
    if isinstance(payload, str):
        return parse.sanitize_text(payload)
    if isinstance(payload, list):
        cleaned: list[object] = []
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                cleaned.append({**item, "text": parse.sanitize_text(item["text"])})
            else:
                cleaned.append(item)
        return cleaned
    return payload


def _voice_spec(voice: object, limits: Limits) -> tuple[dict[str, str], str | None]:
    """Split `voice` into (alias map, default voice).

    A string is a voice name (existing behaviour). A map defines aliases for
    this request only; an `id` entry supplies the default voice.
    """
    if voice is None:
        return {}, None

    if isinstance(voice, str):
        value = voice.strip()
        if not value:
            return {}, None
        try:
            parse.validate_voice(value, limits.voice_chars)
        except parse.MarkupError as exc:
            raise HTTPException(400, str(exc)) from exc
        return {}, value

    if not isinstance(voice, dict):
        raise HTTPException(400, "voice must be a string or an object of alias: voice")

    aliases: dict[str, str] = {}
    default: str | None = None

    for name, target in voice.items():
        if not isinstance(name, str):
            raise HTTPException(400, "voice map must be an object of alias: voice")
        name = name.strip()

        if name == "id":
            default = _voice_id(target, limits)
            continue

        target = _voice_id(target, limits)
        if not name or not target:
            raise HTTPException(400, "voice map entries need an alias and a voice")
        try:
            parse.validate_voice(name, limits.voice_chars)
        except parse.MarkupError as exc:
            raise HTTPException(400, f"invalid voice map: {exc}") from exc
        aliases[name.casefold()] = target

    if len(aliases) > limits.aliases:
        raise HTTPException(400, f"voice map exceeds {limits.aliases} aliases")
    if not aliases and not default:
        raise HTTPException(400, "voice map must define at least one alias")

    return aliases, default or next(iter(aliases.values()))


def _voice_id(value: object, limits: Limits) -> str:
    """Accept a voice name or the OpenAI `{"id": ...}` object."""
    if isinstance(value, dict):
        value = value.get("id")
    if not isinstance(value, str):
        raise HTTPException(400, "voice map values must be a voice name or {'id': ...}")
    value = value.strip()
    try:
        parse.validate_voice(value, limits.voice_chars)
    except parse.MarkupError as exc:
        raise HTTPException(400, str(exc)) from exc
    return value


def _resolve_alias(name: str, aliases: dict[str, str]) -> str:
    """Follow alias -> alias chains, stopping on a cycle or a real voice."""
    seen: set[str] = set()
    current = name
    while True:
        key = current.strip().casefold()
        if key not in aliases or key in seen:
            return current
        seen.add(key)
        current = aliases[key]


def _apply_aliases(turns: list[parse.Turn], aliases: dict[str, str]) -> None:
    if not aliases:
        return
    for turn in turns:
        if turn.voice:
            turn.voice = _resolve_alias(turn.voice, aliases)


def _is_passthrough(payload: object, instructions: str | None) -> bool:
    """Untagged, uninstructed strings are forwarded whole, streaming intact."""
    return isinstance(payload, str) and not parse.has_markup(payload) and not instructions


def _passthrough(body: SpeechRequest, voice: str, text: str, sse: bool = False) -> Response:
    payload: dict[str, object] = {
        "model": SETTINGS.upstream_model,
        "input": text,
        "response_format": body.response_format,
        # SSE needs the engine to emit incrementally; otherwise honour `stream`.
        "stream": True if sse else bool(body.stream),
        "voice": voice,
    }
    if body.speed is not None:
        payload["speed"] = body.speed

    request = http_client().build_request(
        "POST", _upstream_url("audio/speech"), json=payload, headers=_upstream_headers()
    )
    try:
        upstream = http_client().send(request, stream=True)
    except httpx.HTTPError as exc:
        raise HTTPException(502, f"upstream unreachable: {exc.__class__.__name__}") from exc

    if upstream.status_code >= 400:
        try:
            _raise_for_upstream(upstream)
        finally:
            upstream.close()

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
