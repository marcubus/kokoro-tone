"""Shared fake engine and HTTP fixtures for the test suite."""

from __future__ import annotations

import importlib
import json
import math
import struct
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

RATE = 24000


def _tone(seconds: float, frequency: float = 220.0) -> bytes:
    data = bytearray()
    for index in range(int(RATE * seconds)):
        value = 0.2 * math.sin(2 * math.pi * frequency * index / RATE)
        data += struct.pack("<h", int(value * 32767))
    return bytes(data)


PCM = _tone(1.5)
MP3 = b"ID3fakemp3"
AUTH = {"Authorization": "Bearer secret"}


class Upstream(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def do_GET(self):
        if self.path.endswith("/health"):
            # `hide_root_health` models an engine that only answers the
            # versioned base, which is what the fallback probe is for.
            if getattr(self.server, "hide_root_health", False) and not self.path.startswith("/v1"):
                self.send_response(404)
                self.end_headers()
                return
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")
        elif self.path.endswith("/models") and not getattr(self.server, "models_down", False):
            body = json.dumps({"object": "list", "data": []}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        elif self.path.endswith("/audio/voices"):
            body = json.dumps({"voices": ["af_heart"]}).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def do_POST(self):
        length = int(self.headers.get("content-length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        self.server.last_request = body
        self.server.requests.append(body)

        mode = getattr(self.server, "mode", "ok")
        if mode == "400":
            payload = json.dumps({"detail": "Voice 'nope' not found"}).encode()
            self.send_response(400)
            self.send_header("content-type", "application/json")
            self.end_headers()
            self.wfile.write(payload)
            return
        if mode == "401":
            self.send_response(401)
            self.end_headers()
            return

        if body.get("response_format") == "pcm":
            payload, content_type = PCM, "audio/pcm"
        else:
            payload, content_type = MP3, "audio/mpeg"
        self.send_response(200)
        self.send_header("content-type", content_type)
        self.end_headers()
        # Two writes so the SSE path has more than one chunk to wrap.
        half = len(payload) // 2
        self.wfile.write(payload[:half])
        self.wfile.flush()
        self.wfile.write(payload[half:])


def _start_server() -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
    server.last_request = {}
    server.requests = []
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _reload(monkeypatch, server) -> object:
    """Reload the modules that cache settings, in dependency order.

    `settings` is read at import by `routes` (the lexicon) and by `main` (the
    body limit), so both must be rebuilt after it. A module that caches
    `settings.SETTINGS` at import has to be added here.
    """
    monkeypatch.setenv("UPSTREAM_BASE_URL", f"http://127.0.0.1:{server.server_address[1]}/v1")
    monkeypatch.setenv("UPSTREAM_API_KEY", "engine-key")

    import kokoro_tone.main as main
    import kokoro_tone.routes as routes
    import kokoro_tone.settings as settings

    importlib.reload(settings)
    importlib.reload(routes)
    importlib.reload(main)
    return main


@pytest.fixture()
def gateway(monkeypatch):
    server = _start_server()
    monkeypatch.setenv("KOKORO_TONE_API_KEY", "secret")
    monkeypatch.delenv("KOKORO_TONE_API_KEYS", raising=False)
    main = _reload(monkeypatch, server)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield client, server

    server.shutdown()


@pytest.fixture()
def multi_token_gateway(monkeypatch):
    server = _start_server()
    monkeypatch.delenv("KOKORO_TONE_API_KEY", raising=False)
    monkeypatch.setenv("KOKORO_TONE_API_KEYS", "one, two ,one")
    main = _reload(monkeypatch, server)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield client, main

    server.shutdown()


LEXICON_FIXTURE = {
    "defaults": {"rules": {"Lachie": "Locky"}},
    "voices": {
        "bm_lewis": {"rules": {"bass": "base"}, "locale": "en-GB"},
        "am_michael": {"rules": {"bass": "bass"}, "locale": "en-US"},
    },
    "aliases": {"id": "bm_lewis", "narrator": "bm_lewis"},
}


@pytest.fixture()
def lexicon_gateway(monkeypatch, tmp_path):
    path = tmp_path / "lexicon.json"
    path.write_text(json.dumps(LEXICON_FIXTURE), encoding="utf-8")

    server = _start_server()
    monkeypatch.setenv("KOKORO_TONE_API_KEY", "secret")
    monkeypatch.delenv("KOKORO_TONE_API_KEYS", raising=False)
    monkeypatch.setenv("PRONUNCIATION_LEXICON", str(path))
    main = _reload(monkeypatch, server)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield client, server

    server.shutdown()


# A word with two decision rules that disagree and no hint that fires is
# ambiguous, so the caller has to choose a sense.
AMBIGUOUS_FIXTURE = {
    "defaults": {
        "rules": [
            {"match": "bass|music", "say": "base"},
            {"match": "bass|fish", "say": "bas"},
            {"match": "bass", "say": "base", "when": {"after": ["band", "music"]}},
            {"match": "bass", "say": "bas", "when": {"after": ["fish", "swam"]}},
        ],
        "locale": "en-AU",
    },
    "voices": {},
    "aliases": {},
}


@pytest.fixture()
def overlay_gateway(monkeypatch, tmp_path):
    base = tmp_path / "lexicon.json"
    overlay = tmp_path / "custom.json"
    base.write_text(
        json.dumps(
            {
                "defaults": {
                    "rules": [
                        {"match": "Lachie", "say": "default"},
                        {"match": "bass", "say": "base"},
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    overlay.write_text(
        json.dumps({"defaults": {"rules": [{"match": "Lachie", "say": "Custom"}]}}),
        encoding="utf-8",
    )

    server = _start_server()
    monkeypatch.setenv("KOKORO_TONE_API_KEY", "secret")
    monkeypatch.delenv("KOKORO_TONE_API_KEYS", raising=False)
    monkeypatch.setenv("PRONUNCIATION_LEXICON", str(base))
    monkeypatch.setenv("PRONUNCIATION_OVERRIDES", str(overlay))
    monkeypatch.setenv("NORMALISE_DEFAULT", "true")
    main = _reload(monkeypatch, server)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield client, server

    server.shutdown()


@pytest.fixture()
def ambiguous_gateway(monkeypatch, tmp_path):
    path = tmp_path / "lexicon.json"
    path.write_text(json.dumps(AMBIGUOUS_FIXTURE), encoding="utf-8")

    server = _start_server()
    monkeypatch.setenv("KOKORO_TONE_API_KEY", "secret")
    monkeypatch.delenv("KOKORO_TONE_API_KEYS", raising=False)
    monkeypatch.setenv("PRONUNCIATION_LEXICON", str(path))
    monkeypatch.setenv("NORMALISE_DEFAULT", "true")
    main = _reload(monkeypatch, server)

    from fastapi.testclient import TestClient

    with TestClient(main.app) as client:
        yield client, server

    server.shutdown()
