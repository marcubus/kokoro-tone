from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from support import AUTH, MP3, _start_server, build_app


def _metrics(reader):
    data = reader.get_metrics_data()
    found = {}
    if data is None:
        return found
    for resource_metrics in data.resource_metrics:
        for scope_metrics in resource_metrics.scope_metrics:
            for metric in scope_metrics.metrics:
                found[metric.name] = metric
    return found


def _points(metric):
    return list(metric.data.data_points)


def _attributes(metric):
    return [dict(point.attributes) for point in _points(metric)]


def test_rendered_speech_records_work_and_cost(metrics_gateway):
    client, _, reader = metrics_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "[voice:af_bella] [emotion:warm] Hello there.",
            "response_format": "wav",
        },
    )
    assert response.status_code == 200

    metrics = _metrics(reader)
    assert "kokoro_tone.speech.requests" in metrics
    assert "kokoro_tone.speech.duration" in metrics
    assert "kokoro_tone.speech.audio.duration" in metrics
    assert "kokoro_tone.speech.chars" in metrics
    assert "kokoro_tone.upstream.request.duration" in metrics
    assert "kokoro_tone.ffmpeg.duration" in metrics

    speech = _attributes(metrics["kokoro_tone.speech.requests"])
    assert any(
        item.get("kokoro_tone.passthrough") is False and item.get("kokoro_tone.format") == "wav"
        for item in speech
    )

    stages = {
        item.get("kokoro_tone.stage")
        for item in _attributes(metrics["kokoro_tone.ffmpeg.duration"])
    }
    assert {"prosody", "tone", "measure", "limit", "encode"} <= stages

    modes = {
        item.get("kokoro_tone.upstream.mode")
        for item in _attributes(metrics["kokoro_tone.upstream.request.duration"])
    }
    assert "synth" in modes


def test_passthrough_is_counted_as_speech(metrics_gateway):
    client, _, reader = metrics_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "voice": "af_heart"},
    )
    assert response.status_code == 200
    assert response.content == MP3

    metrics = _metrics(reader)
    speech = _attributes(metrics["kokoro_tone.speech.requests"])
    assert any(item.get("kokoro_tone.passthrough") is True for item in speech)


def test_http_metrics_use_the_route_template(metrics_gateway):
    client, _, reader = metrics_gateway
    client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "[voice:af_bella] Hello."},
    )

    metrics = _metrics(reader)
    routes = {
        item.get("http.route") for item in _attributes(metrics["http.server.request.duration"])
    }
    assert "/v1/audio/speech" in routes
    assert "unmatched" not in routes


def test_unmatched_route_is_bounded(metrics_gateway):
    client, _, reader = metrics_gateway
    response = client.get("/not-a-real-path")
    assert response.status_code == 404

    metrics = _metrics(reader)
    routes = {
        item.get("http.route") for item in _attributes(metrics["http.server.request.duration"])
    }
    assert "unmatched" in routes


def test_auth_failure_is_counted(metrics_gateway):
    client, _, reader = metrics_gateway
    response = client.post("/v1/audio/speech", json={"input": "hi"})
    assert response.status_code == 401

    metrics = _metrics(reader)
    errors = _attributes(metrics["kokoro_tone.errors"])
    assert any(item.get("error.type") == "auth" for item in errors)


def test_metrics_never_carry_the_script_or_voice(metrics_gateway):
    client, _, reader = metrics_gateway
    client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "[voice:af_bella] Secret words.", "response_format": "wav"},
    )

    metrics = _metrics(reader)
    for metric in metrics.values():
        for point in _points(metric):
            attributes = dict(point.attributes)
            assert "voice" not in attributes
            assert "text" not in attributes
            for value in attributes.values():
                assert "Secret words" not in str(value)
                assert "af_bella" not in str(value)


def test_metrics_endpoint_is_not_exposed(metrics_gateway):
    client, _, _ = metrics_gateway
    assert client.get("/metrics").status_code == 404


def test_disabled_sdk_still_serves(monkeypatch):
    server = _start_server()
    app = build_app(
        monkeypatch,
        server,
        OTEL_SDK_DISABLED="true",
        KOKORO_TONE_API_KEY="secret",
        KOKORO_TONE_API_KEYS=None,
    )
    with TestClient(app) as client:
        response = client.post(
            "/v1/audio/speech",
            headers=AUTH,
            json={"input": "[voice:af_bella] Hello."},
        )
        assert response.status_code == 200
    server.shutdown()


@pytest.mark.parametrize("format_name", ["mp3", "wav", "flac", "opus", "aac", "pcm"])
def test_every_output_format_is_counted(metrics_gateway, format_name):
    client, _, reader = metrics_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "[voice:af_bella] Hello.",
            "response_format": format_name,
        },
    )
    assert response.status_code == 200

    metrics = _metrics(reader)
    formats = {
        item.get("kokoro_tone.format")
        for item in _attributes(metrics["kokoro_tone.speech.requests"])
    }
    assert format_name in formats


def test_format_labels_are_normalised(metrics_gateway):
    client, _, reader = metrics_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "[voice:af_bella] Hello.", "response_format": "MP3"},
    )
    assert response.status_code == 200

    metrics = _metrics(reader)
    formats = {
        item.get("kokoro_tone.format")
        for item in _attributes(metrics["kokoro_tone.speech.requests"])
    }
    assert "mp3" in formats
    assert "MP3" not in formats
