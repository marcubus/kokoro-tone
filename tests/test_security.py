"""The gateway treats client input as data, never as code.

These tests cover the two halves of that claim: injection-shaped input is
rejected or neutralised, and the ffmpeg filter chain can only ever be built
from the known grammar.
"""

import pytest

from support import AUTH


@pytest.mark.parametrize(
    "text",
    [
        "[voice:__import__('os').system('id')] Hi.",
        "[rate:1);import os] Hi.",
        "[pause:1s;rm -rf /] Hi.",
        "[emotion:os.system] Hi.",
        "[voice:a b] Hi.",
        "[voice:$(id)] Hi.",
    ],
)
def test_injection_shaped_markup_is_rejected(gateway, text):
    client, _ = gateway
    response = client.post("/v1/audio/speech", headers=AUTH, json={"input": text})

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


@pytest.mark.parametrize(
    "voice",
    [
        {"narrator": "a;rm -rf /"},
        {"narrator": "__import__('os')"},
        {"bad alias": "af_bella"},
        {"narrator": {"id": "af_bella; id"}},
    ],
)
def test_alias_map_values_are_validated(gateway, voice):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "hello", "voice": voice}
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_turn_array_voice_is_validated(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": [{"text": "hello", "voice": "x; id"}]},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_model_is_never_forwarded(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "model": "$(id)"},
    )

    assert response.status_code == 400
    assert server.requests == []


def test_nul_and_control_bytes_are_stripped_before_the_engine(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "he\x00llo\x07 there"},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "hello there"


def test_too_many_turns_is_rejected(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": [{"text": "x"} for _ in range(101)]},
    )

    assert response.status_code == 400
    assert "turns" in response.json()["error"]["message"]


def test_too_many_aliases_is_rejected(gateway):
    client, _ = gateway
    aliases = {f"alias{index}": "af_bella" for index in range(65)}
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "hello", "voice": aliases}
    )

    assert response.status_code == 400
    assert "aliases" in response.json()["error"]["message"]


def test_total_pause_is_capped(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "[pause:30s] a [pause:30s] b [pause:1s] c"},
    )

    assert response.status_code == 400
    assert "total pause" in response.json()["error"]["message"]


def test_oversized_body_is_413(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "pad": "x" * 1_100_000},
    )

    assert response.status_code == 413
    assert response.json()["error"]["type"] == "invalid_request_error"


def test_error_echo_is_truncated(gateway):
    client, _ = gateway
    long_voice = "v" * 4_000
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "hello", "voice": long_voice}
    )

    assert response.status_code == 400
    assert response.json()["error"]["message"].count("v") <= 40


def test_filter_chain_refuses_unexpected_text():
    from kokoro_tone import audio

    with pytest.raises(audio.AudioError):
        audio._filters(["volume=1dB; rm -rf /"])


def test_every_preset_filter_matches_the_guard():
    from kokoro_tone import audio, prosody

    for preset in prosody.PRESETS.values():
        audio._filters(list(preset.filters))


@pytest.mark.parametrize(
    "pronunciations",
    [
        {"hello": "[voice:af_heart]"},
        {"hello": "x" * 201},
        {"hello": "a\x00b"},
        [{"match": "hello", "say": "hi", "extra": "y"}],
        [{"match": "hello", "say": "hi", "when": {"within": 99}}],
        {"bad alias": []},
        {"*": {"hello": "hi"}, "bad alias": []},
    ],
)
def test_injection_shaped_pronunciations_are_rejected(gateway, pronunciations):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "pronunciations": pronunciations},
    )

    assert response.status_code == 400
    assert response.json()["error"]["type"] == "invalid_request_error"
    assert server.requests == []


def test_a_replacement_is_spoken_as_text_not_evaluated(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "pronunciations": {"hello": "__import__('os')"}},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "__import__('os')"


def test_too_many_pronunciation_rules_is_rejected(gateway):
    client, _ = gateway
    value = {f"word{index}": "x" for index in range(257)}
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "hello", "pronunciations": value}
    )

    assert response.status_code == 400
    assert "pronunciations" in response.json()["error"]["message"]


def test_unknown_normalise_keys_are_rejected(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "normalise": {"numbers": True, "extra": True}},
    )

    assert response.status_code == 400
    assert "normalise" in response.json()["error"]["message"]
