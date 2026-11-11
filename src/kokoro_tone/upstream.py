"""The engine seam: every HTTP call to Kokoro-FastAPI lives here.

`probe`, `list_voices`, `synthesise` and `open_stream` are the only places the
gateway touches the engine. Failures become `UpstreamError`, which the routes
map to the OpenAI error envelope.
"""

from __future__ import annotations

import httpx

from . import settings


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _upstream_headers() -> dict[str, str]:
    if settings.SETTINGS.upstream_api_key:
        return {"Authorization": f"Bearer {settings.SETTINGS.upstream_api_key}"}
    return {}


def _upstream_url(path: str) -> str:
    return f"{settings.SETTINGS.upstream_base_url}/{path.lstrip('/')}"


def upstream_health_urls() -> tuple[str, ...]:
    """Where to ask whether the engine is up, best first.

    Kokoro-FastAPI answers `GET /health` at the root and has no `/v1/health`, so
    probing `{base}/health` reported a healthy engine as degraded. The root is
    tried first; `{base}/models` is the fallback for an OpenAI-shaped upstream
    (the OpenRouter seam), which has no health route but does answer the
    versioned base when the bearer is right.
    """
    base = settings.SETTINGS.upstream_base_url.rstrip("/")
    root = base[: -len("/v1")] if base.endswith("/v1") else base
    return (f"{root}/health", f"{base}/models")


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


def probe() -> tuple[bool, str]:
    """Ask whether the engine is up, best URL first. Returns (ok, detail)."""
    detail = "upstream unreachable"
    for url in upstream_health_urls():
        try:
            response = http_client().get(url, headers=_upstream_headers(), timeout=5.0)
        except httpx.HTTPError as exc:
            detail = f"upstream unreachable: {exc.__class__.__name__}"
            continue
        if response.status_code == 200:
            return True, "ok"
        detail = f"upstream returned {response.status_code} from {url}"
    return False, detail


def list_voices() -> dict:
    """The engine's voice inventory, with its failures mapped to UpstreamError."""
    try:
        response = http_client().get(
            _upstream_url("audio/voices"), headers=_upstream_headers(), timeout=15.0
        )
    except httpx.HTTPError as exc:
        raise UpstreamError(502, f"upstream unreachable: {exc.__class__.__name__}") from exc
    if response.status_code != 200:
        raise UpstreamError(502, f"upstream returned {response.status_code}")
    return response.json()


def _speech_payload(
    text: str,
    voice: str | None,
    response_format: str,
    speed: float | None,
    stream: bool,
    volume_multiplier: float | None = None,
) -> dict[str, object]:
    """The engine's request body, built in exactly one place."""
    payload: dict[str, object] = {
        "model": settings.SETTINGS.upstream_model,
        "input": text,
        "voice": voice or settings.SETTINGS.default_voice,
        "response_format": response_format,
        "stream": stream,
    }
    if speed is not None:
        payload["speed"] = speed
    if volume_multiplier is not None:
        payload["volume_multiplier"] = volume_multiplier
    return payload


def open_stream(
    text: str,
    voice: str | None,
    response_format: str,
    speed: float | None,
    stream: bool,
    volume_multiplier: float | None = None,
) -> httpx.Response:
    """Send a streaming speech request and return the open response.

    The caller owns the response and must close it after consuming the body.
    """
    request = http_client().build_request(
        "POST",
        _upstream_url("audio/speech"),
        json=_speech_payload(text, voice, response_format, speed, stream, volume_multiplier),
        headers=_upstream_headers(),
    )
    try:
        response = http_client().send(request, stream=True)
    except httpx.HTTPError as exc:
        raise UpstreamError(502, f"upstream unreachable: {exc.__class__.__name__}") from exc
    if response.status_code >= 400:
        try:
            _raise_for_upstream(response)
        finally:
            response.close()
    return response


def synthesise(text: str, voice: str | None, volume_multiplier: float | None = None) -> bytes:
    try:
        response = http_client().post(
            _upstream_url("audio/speech"),
            json=_speech_payload(text, voice, "pcm", None, False, volume_multiplier),
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


def close_client() -> None:
    """Close the shared client, if one was opened."""
    global _client
    if _client is not None:
        _client.close()
        _client = None
