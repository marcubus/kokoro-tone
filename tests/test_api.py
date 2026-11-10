import base64
import json

import pytest

from support import AUTH, MP3, PCM


def _sse_events(body: str) -> list[tuple[str, dict]]:
    events = []
    for block in body.strip().split("\n\n"):
        lines = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        events.append((lines.get("event", ""), json.loads(lines["data"])))
    return events


def test_missing_auth_uses_openai_error_envelope(gateway):
    client, _ = gateway
    response = client.post("/v1/audio/speech", json={"input": "hi"})

    assert response.status_code == 401
    error = response.json()["error"]
    assert error["type"] == "authentication_error"
    assert error["code"] == "invalid_api_key"


def test_validation_error_is_400_with_param(gateway):
    client, _ = gateway
    response = client.post("/v1/audio/speech", headers=AUTH, json={"voice": "af_heart"})

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert error["param"] == "input"


def test_bad_stream_format_is_rejected(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "hi", "stream_format": "pcm"}
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_bad_response_format_is_rejected(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "hi", "response_format": "ogg"}
    )

    assert response.status_code == 400
    assert "response_format" in response.json()["error"]["message"]


def test_passthrough_sse_wraps_upstream_audio(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "voice": "af_heart", "stream_format": "sse"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _sse_events(response.text)
    assert events[-1][0] == "speech.audio.done"
    assert all(name == "speech.audio.delta" for name, _ in events[:-1])

    audio_bytes = b"".join(
        base64.b64decode(payload["audio"])
        for name, payload in events
        if name == "speech.audio.delta"
    )
    assert audio_bytes == MP3
    assert server.last_request["stream"] is True
    assert events[-1][1]["usage"]["input_tokens"] >= 1


def test_rendered_sse_returns_mastered_audio(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "[voice:af_bella] [emotion:warm] Hello there.",
            "response_format": "wav",
            "stream_format": "sse",
        },
    )

    assert response.status_code == 200
    events = _sse_events(response.text)
    assert events[-1][0] == "speech.audio.done"
    audio = b"".join(
        base64.b64decode(payload["audio"])
        for name, payload in events
        if name == "speech.audio.delta"
    )
    assert audio[:4] == b"RIFF"


def test_binary_response_is_unchanged(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "[voice:af_bella] Hello.", "response_format": "wav"},
    )

    assert response.status_code == 200
    assert response.headers["content-type"] == "audio/wav"
    assert response.content[:4] == b"RIFF"


def test_passthrough_is_not_mastered(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "response_format": "pcm"},
    )

    assert response.status_code == 200
    assert response.content == PCM


def test_rendered_request_is_mastered(gateway):
    from kokoro_tone import audio

    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "[voice:af_bella] Hello.", "response_format": "pcm"},
    )

    assert response.status_code == 200
    assert response.content != PCM
    lufs, true_peak = audio.measure_loudness(response.content)
    assert lufs == pytest.approx(-16.0, abs=1.0)
    assert true_peak <= -1.0 + 0.2


def test_stream_true_is_forwarded_to_the_engine(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "hello", "stream": True}
    )

    assert response.status_code == 200
    assert response.content == MP3
    assert server.last_request["stream"] is True


def test_upstream_400_is_passed_through(gateway):
    client, server = gateway
    server.mode = "400"
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "[voice:af_bella] Hello."}
    )

    assert response.status_code == 400
    error = response.json()["error"]
    assert error["type"] == "invalid_request_error"
    assert "not found" in error["message"]


def test_upstream_auth_failure_is_502(gateway):
    client, server = gateway
    server.mode = "401"
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "[voice:af_bella] Hello."}
    )

    assert response.status_code == 502
    assert response.json()["error"]["type"] == "server_error"


def test_voice_string_still_reaches_the_engine(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "hello", "voice": "af_sky"}
    )

    assert response.status_code == 200
    assert server.last_request["voice"] == "af_sky"


def test_voice_map_resolves_alias_tags(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "[voice:narrator] Hello.",
            "voice": {"narrator": "af_bella", "hero": "am_michael"},
        },
    )

    assert response.status_code == 200
    assert server.last_request["voice"] == "af_bella"


def test_voice_map_defaults_to_its_first_entry(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "hello",
            "voice": {"narrator": "af_bella", "hero": "am_michael"},
        },
    )

    assert response.status_code == 200
    assert server.last_request["voice"] == "af_bella"


def test_voice_map_resolves_turn_voices_case_insensitively(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": [
                {"text": "One.", "voice": "hero"},
                {"text": "Two.", "voice": "NARRATOR"},
            ],
            "voice": {"narrator": "af_bella", "hero": "am_michael"},
        },
    )

    assert response.status_code == 200
    assert [request["voice"] for request in server.requests] == ["am_michael", "af_bella"]


def test_voice_map_follows_alias_chains(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "[voice:a] Hello.",
            "voice": {"a": "b", "b": "af_sky"},
        },
    )

    assert response.status_code == 200
    assert server.last_request["voice"] == "af_sky"


def test_unknown_alias_is_left_to_the_engine(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "[voice:mystery] Hello.", "voice": {"narrator": "af_bella"}},
    )

    assert response.status_code == 200
    assert server.last_request["voice"] == "mystery"


def test_empty_voice_map_is_rejected(gateway):
    client, _ = gateway
    response = client.post("/v1/audio/speech", headers=AUTH, json={"input": "hello", "voice": {}})

    assert response.status_code == 400
    assert "at least one alias" in response.json()["error"]["message"]


def test_voice_map_with_invalid_target_is_rejected(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "voice": {"narrator": "bad voice"}},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_voice_id_object_sets_the_default(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "voice": {"id": "af_bella"}},
    )

    assert response.status_code == 200
    assert server.last_request["voice"] == "af_bella"


def test_voice_map_id_entry_is_the_default(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "hello",
            "voice": {"id": "am_puck", "narrator": "af_bella"},
        },
    )

    assert response.status_code == 200
    assert server.last_request["voice"] == "am_puck"


def test_voice_map_alias_values_may_be_id_objects(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "[voice:narrator] Hello.",
            "voice": {"narrator": {"id": "af_sky"}},
        },
    )

    assert response.status_code == 200
    assert server.last_request["voice"] == "af_sky"


def test_models_lists_only_the_real_model(gateway):
    client, _ = gateway
    response = client.get("/v1/models", headers=AUTH)

    assert response.status_code == 200
    assert [entry["id"] for entry in response.json()["data"]] == ["kokoro-v1_0"]


def test_gpt_model_name_is_rejected(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "model": "gpt-4o-mini-tts"},
    )

    assert response.status_code == 400
    message = response.json()["error"]["message"]
    assert "Kokoro" in message
    assert "GPT" in message


@pytest.mark.parametrize("model", ["kokoro-v1_0", "kokoro", "tts-1", "tts-1-hd"])
def test_accepted_model_aliases_reach_the_engine_as_kokoro(gateway, model):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "model": model},
    )

    assert response.status_code == 200
    assert server.last_request["model"] == "kokoro"


def test_unknown_model_is_rejected(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "model": "not-a-model"},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
