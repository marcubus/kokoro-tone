"""Reject oversized bodies before FastAPI parses them.

The body is buffered so the limit also holds for chunked requests without a
content-length. Replay blocks after the body is delivered, which is what a real
`receive` does, so streaming responses keep working.
"""

from __future__ import annotations

import asyncio
import json

from .errors import error_body


class BodyLimitMiddleware:
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
