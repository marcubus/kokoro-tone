"""The engine seam: every HTTP call to Kokoro-FastAPI lives here.

`Upstream` owns the client and its lifecycle. `probe`, `voices`, `synthesise`
and `open_stream` are the only places the gateway touches the engine. Failures
become `UpstreamError`, which the routes map to the OpenAI error envelope.
"""

from __future__ import annotations

import threading
import time

import httpx2

from .settings import Settings
from .telemetry import Telemetry

VOICES_TTL_SECONDS = 300


class UpstreamError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _raise_for_upstream(response: httpx2.Response) -> None:
    if response.status_code < 400:
        return
    if response.status_code == 400:
        raise UpstreamError(400, _detail(response))
    if response.status_code in (401, 403):
        raise UpstreamError(502, "upstream rejected the gateway credentials")
    raise UpstreamError(502, f"upstream returned {response.status_code}: {_detail(response)}")


def _detail(response: httpx2.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:400] or "no detail"
    if isinstance(body, dict):
        for key in ("detail", "error", "message"):
            if key in body:
                return str(body[key])[:400]
    return str(body)[:400]


class Upstream:
    """One engine client for the life of the app."""

    def __init__(self, settings: Settings, telemetry: Telemetry | None = None) -> None:
        self._settings = settings
        self._telemetry = telemetry
        self._client = httpx2.Client(timeout=httpx2.Timeout(180.0, connect=10.0))
        self._voices_lock = threading.Lock()
        self._voices: dict | None = None
        self._voices_at = 0.0
        if self._telemetry is not None:
            self._telemetry.upstream_up.set(0)

    def close(self) -> None:
        self._client.close()

    # ------------------------------------------------------------ telemetry

    def _record_request(self, mode: str, status: int, started: float) -> None:
        if self._telemetry is None:
            return
        attributes: dict[str, str | int] = {
            "kokoro_tone.upstream.mode": mode,
            "http.response.status_code": status,
        }
        self._telemetry.upstream_duration.record(time.perf_counter() - started, attributes)

    def _mark_success(self) -> None:
        if self._telemetry is not None:
            self._telemetry.upstream_up.set(1)

    def _mark_down(self) -> None:
        if self._telemetry is not None:
            self._telemetry.upstream_up.set(0)

    # ------------------------------------------------------------ addressing

    def _headers(self) -> dict[str, str]:
        if self._settings.upstream_api_key:
            return {"Authorization": f"Bearer {self._settings.upstream_api_key}"}
        return {}

    def _url(self, path: str) -> str:
        return f"{self._settings.upstream_base_url}/{path.lstrip('/')}"

    def health_urls(self) -> tuple[str, ...]:
        """Where to ask whether the engine is up, best first.

        Kokoro-FastAPI answers `GET /health` at the root and has no `/v1/health`,
        so probing `{base}/health` reported a healthy engine as degraded. The
        root is tried first; `{base}/models` is the fallback for an OpenAI-shaped
        upstream (the OpenRouter seam), which has no health route but does answer
        the versioned base when the bearer is right.
        """
        base = self._settings.upstream_base_url.rstrip("/")
        root = base[: -len("/v1")] if base.endswith("/v1") else base
        return (f"{root}/health", f"{base}/models")

    # ---------------------------------------------------------------- reads

    def probe(self) -> tuple[bool, str]:
        """Ask whether the engine is up, best URL first. Returns (ok, detail)."""
        detail = "upstream unreachable"
        for url in self.health_urls():
            try:
                response = self._client.get(url, headers=self._headers(), timeout=5.0)
            except httpx2.HTTPError as exc:
                detail = f"upstream unreachable: {exc.__class__.__name__}"
                continue
            if response.status_code == 200:
                self._mark_success()
                return True, "ok"
            detail = f"upstream returned {response.status_code} from {url}"
        self._mark_down()
        return False, detail

    def voices(self) -> dict:
        """The engine's voice inventory, cached briefly so polling is cheap."""
        now = time.monotonic()
        with self._voices_lock:
            if self._voices is not None and now - self._voices_at < VOICES_TTL_SECONDS:
                return self._voices
        # Load outside the lock: a slow engine must not block other readers.
        try:
            data = self._list_voices()
        except UpstreamError as exc:
            if exc.status >= 500:
                self._mark_down()
            raise
        with self._voices_lock:
            self._voices = data
            self._voices_at = time.monotonic()
        self._mark_success()
        return data

    def _list_voices(self) -> dict:
        try:
            response = self._client.get(
                self._url("audio/voices"), headers=self._headers(), timeout=15.0
            )
        except httpx2.HTTPError as exc:
            raise UpstreamError(502, f"upstream unreachable: {exc.__class__.__name__}") from exc
        if response.status_code != 200:
            raise UpstreamError(502, f"upstream returned {response.status_code}")
        return dict(response.json())

    # --------------------------------------------------------------- speech

    def _speech_payload(
        self,
        text: str,
        voice: str | None,
        response_format: str,
        speed: float | None,
        stream: bool,
        volume_multiplier: float | None,
    ) -> dict[str, object]:
        """The engine's request body, built in exactly one place."""
        payload: dict[str, object] = {
            "model": self._settings.upstream_model,
            "input": text,
            "voice": voice or self._settings.default_voice,
            "response_format": response_format,
            "stream": stream,
        }
        if speed is not None:
            payload["speed"] = speed
        if volume_multiplier is not None:
            payload["volume_multiplier"] = volume_multiplier
        return payload

    def open_stream(
        self,
        text: str,
        voice: str | None,
        response_format: str,
        speed: float | None,
        stream: bool,
        volume_multiplier: float | None = None,
    ) -> httpx2.Response:
        """Send a streaming speech request and return the open response.

        The caller owns the response and must close it after consuming the body.
        """
        request = self._client.build_request(
            "POST",
            self._url("audio/speech"),
            json=self._speech_payload(
                text, voice, response_format, speed, stream, volume_multiplier
            ),
            headers=self._headers(),
        )
        started = time.perf_counter()
        status = 502
        try:
            response = self._client.send(request, stream=True)
            if response.status_code >= 400:
                try:
                    _raise_for_upstream(response)
                finally:
                    response.close()
            status = 200
            self._mark_success()
            return response
        except UpstreamError as exc:
            status = exc.status
            if status >= 500:
                self._mark_down()
            raise
        except httpx2.HTTPError as exc:
            self._mark_down()
            raise UpstreamError(502, f"upstream unreachable: {exc.__class__.__name__}") from exc
        finally:
            self._record_request("stream", status, started)

    def synthesise(
        self, text: str, voice: str | None, volume_multiplier: float | None = None
    ) -> bytes:
        started = time.perf_counter()
        status = 502
        try:
            response = self._client.post(
                self._url("audio/speech"),
                json=self._speech_payload(text, voice, "pcm", None, False, volume_multiplier),
                headers=self._headers(),
            )
            _raise_for_upstream(response)
            status = 200
            self._mark_success()
            return response.content
        except UpstreamError as exc:
            status = exc.status
            if status >= 500:
                self._mark_down()
            raise
        except httpx2.HTTPError as exc:
            self._mark_down()
            raise UpstreamError(502, f"upstream unreachable: {exc.__class__.__name__}") from exc
        finally:
            self._record_request("synth", status, started)
