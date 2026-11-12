"""Reject oversized bodies before FastAPI parses them.

The body is buffered so the limit also holds for chunked requests without a
content-length. Replay blocks after the body is delivered, which is what a real
`receive` does, so streaming responses keep working.
"""

from __future__ import annotations

import asyncio
import json
import time

from .errors import error_body
from .telemetry import Telemetry

# Paths whose route is known even when the request is rejected before routing.
_KNOWN_PATHS = frozenset({"/health", "/v1/models", "/v1/audio/voices", "/v1/audio/speech"})


class BodyLimitMiddleware:
    def __init__(self, app, max_bytes: int, telemetry: Telemetry | None = None) -> None:
        self.app = app
        self.max_bytes = max_bytes
        self.telemetry = telemetry

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
        if self.telemetry is not None:
            self.telemetry.errors.add(1, {"error.type": "body_too_large"})
        payload = json.dumps(
            error_body(
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


class TelemetryMiddleware:
    """Record one HTTP request as OTel server metrics.

    It is the outermost middleware, so a body-limit rejection and an auth
    failure are counted too. The route label is the route template, never the
    raw path: a 404 is `unmatched`, which keeps the series bounded.
    """

    def __init__(self, app, telemetry: Telemetry) -> None:
        self.app = app
        self.telemetry = telemetry

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        scope.setdefault("state", {})
        method = scope.get("method", "GET")
        scheme = scope.get("scheme", "http")
        server = scope.get("server") or ("", 0)
        base = {
            "http.request.method": method,
            "url.scheme": scheme,
            "server.address": server[0],
            "server.port": server[1],
        }
        self.telemetry.http_server_active_requests.add(1, base)
        started = time.perf_counter()
        status: int | None = None

        async def counting_send(message):
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
            await send(message)

        try:
            await self.app(scope, receive, counting_send)
        finally:
            route = scope.get("route")
            route_path = getattr(route, "path", None)
            if route_path is None:
                path = scope.get("path", "")
                route_path = path if path in _KNOWN_PATHS else "unmatched"
            attributes = {
                **base,
                "http.route": route_path,
            }
            if status is not None:
                attributes["http.response.status_code"] = status
            if scope["state"].get("streaming"):
                attributes["kokoro_tone.streaming"] = True
            self.telemetry.http_server_request_duration.record(
                time.perf_counter() - started, attributes
            )
            self.telemetry.http_server_active_requests.add(-1, base)
