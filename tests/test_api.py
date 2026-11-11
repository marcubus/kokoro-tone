import base64
import json

import pytest

from kokoro_tone import levelling
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
    assert server.last_request["volume_multiplier"] == pytest.approx(
        levelling.multiplier("af_heart")
    )
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


def test_passthrough_levels_the_voice(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "hello", "voice": "af_nova"}
    )

    assert response.status_code == 200
    assert server.last_request["volume_multiplier"] == pytest.approx(
        levelling.multiplier("af_nova")
    )


def test_the_default_voice_is_levelled(gateway):
    client, server = gateway
    response = client.post("/v1/audio/speech", headers=AUTH, json={"input": "hello"})

    assert response.status_code == 200
    assert server.last_request["voice"] == "af_heart"
    assert server.last_request["volume_multiplier"] == pytest.approx(
        levelling.multiplier("af_heart")
    )


def test_rendered_turns_are_levelled_per_voice(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "[voice:af_nova] One. [voice:am_adam] Two.",
            "response_format": "wav",
        },
    )

    assert response.status_code == 200
    assert [request["voice"] for request in server.requests] == ["af_nova", "am_adam"]
    assert [request["volume_multiplier"] for request in server.requests] == pytest.approx(
        [levelling.multiplier("af_nova"), levelling.multiplier("am_adam")]
    )


def test_a_blend_is_levelled_by_its_weighted_members(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "voice": "af_bella(2)+af_sky(1)"},
    )

    assert response.status_code == 200
    assert server.last_request["volume_multiplier"] == pytest.approx(
        levelling.multiplier("af_bella(2)+af_sky(1)")
    )


def test_voice_levelling_can_be_disabled(unlevelled_gateway):
    client, server = unlevelled_gateway
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "hello", "voice": "af_nova"}
    )

    assert response.status_code == 200
    assert "volume_multiplier" not in server.last_request


def test_health_reports_voice_levelling(gateway):
    client, _ = gateway
    payload = client.get("/health").json()

    assert payload["voice_levelling"]["enabled"] is True
    assert payload["voice_levelling"]["voices"] == len(levelling.VOICE_GAIN_DB)


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


# ---------------------------------------------------------------- pronunciations


def test_pronunciations_rewrite_the_passthrough_text(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "The bass player", "pronunciations": {"bass": "base"}},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "The base player"


def test_normalise_expands_acronyms_and_digits(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "In 2026 AWS cost 15%.", "normalise": True},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == ("In 2026 Aigh Double You Ess cost fifteen per cent.")


def test_normalise_layers_can_be_selected(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "AWS 15%", "normalise": {"numbers": True}},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "AWS fifteen per cent"


def test_pronunciations_do_not_touch_tag_values(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "[voice:af_heart] Hello.",
            "pronunciations": {"af_heart": "nope", "voice": "nope"},
        },
    )

    assert response.status_code == 200
    assert server.last_request["voice"] == "af_heart"
    assert server.last_request["input"] == "Hello."


def test_per_alias_pronunciations_follow_the_voice(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "[voice:narrator] The bass.\n[voice:guest] The bass.",
            "voice": {"id": "narrator", "narrator": "bm_lewis", "guest": "am_michael"},
            "pronunciations": {
                "narrator": [{"match": "bass", "say": "base"}],
                "guest": [{"match": "bass", "say": "fish"}],
            },
        },
    )

    assert response.status_code == 200
    assert [request["input"] for request in server.requests] == [
        "The base.",
        "The fish.",
    ]


def test_an_inline_voice_tag_uses_that_voices_set(lexicon_gateway):
    client, server = lexicon_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "[voice:bm_lewis] The bass.\n[voice:am_michael] The bass.",
            "normalise": True,
        },
    )

    assert response.status_code == 200
    assert [request["input"] for request in server.requests] == [
        "The base.",
        "The bass.",
    ]
    assert [request["voice"] for request in server.requests] == ["bm_lewis", "am_michael"]


def test_server_aliases_and_defaults_apply_without_a_request_map(lexicon_gateway):
    client, server = lexicon_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "Lachie said hi", "normalise": True},
    )

    assert response.status_code == 200
    assert server.last_request["voice"] == "bm_lewis"
    assert server.last_request["input"] == "Locky said hi"


def test_a_request_voice_overrides_a_server_alias(lexicon_gateway):
    client, server = lexicon_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hi", "voice": "af_bella"},
    )

    assert response.status_code == 200
    assert server.last_request["voice"] == "af_bella"


def test_brackets_in_a_replacement_are_rejected(gateway):
    client, server = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "pronunciations": {"hello": "[voice:af_heart]"}},
    )

    assert response.status_code == 400
    assert server.requests == []


def test_a_rewrite_that_exceeds_the_input_cap_is_rejected(gateway):
    client, _ = gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "x" * 30,
            "pronunciations": [{"match": "x", "say": "y" * 199, "whole_word": False}],
        },
    )

    assert response.status_code == 400
    assert "input exceeds" in response.json()["error"]["message"]


def test_the_health_probe_asks_the_engine_root(gateway):
    """Kokoro-FastAPI serves /health at the root, not under the /v1 base."""
    client, server = gateway
    port = server.server_address[1]

    assert client.app.state.ctx.upstream.health_urls() == (
        f"http://127.0.0.1:{port}/health",
        f"http://127.0.0.1:{port}/v1/models",
    )


def test_health_falls_back_to_the_versioned_base(gateway):
    """An OpenAI-shaped upstream has no /health but does answer /models."""
    client, server = gateway
    server.hide_root_health = True

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["status"] == "ok"


def test_health_is_degraded_when_neither_probe_answers(gateway):
    client, server = gateway
    server.hide_root_health = True
    server.models_down = True

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "degraded"


def test_health_reports_the_normalise_configuration(lexicon_gateway):
    client, _ = lexicon_gateway
    payload = client.get("/health").json()

    assert payload["normalise"]["locale"] == "en-AU"
    assert payload["normalise"]["rules"] == 1
    assert payload["normalise"]["voice_sets"] == 2
    assert payload["normalise"]["server_aliases"] == 1


# ---------------------------------------------------------------- ambiguity


def test_ambiguity_guesses_by_default(ambiguous_gateway):
    """No error: pick the first candidate, render, and say what was assumed."""
    client, server = ambiguous_gateway
    response = client.post("/v1/audio/speech", headers=AUTH, json={"input": "The bass was loud."})

    assert response.status_code == 200
    assert server.last_request["input"] == "The base was loud."
    assert response.headers["x-kokoro-tone-guessed"] == "bass=music"


def test_a_resolved_word_is_not_reported_as_guessed(ambiguous_gateway):
    client, server = ambiguous_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "He plays bass in the band."},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "He plays base in the band."
    assert "x-kokoro-tone-guessed" not in response.headers


def test_ambiguity_ignore_leaves_the_engine_default(ambiguous_gateway):
    client, server = ambiguous_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "The bass was loud.", "ambiguity": "ignore"},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "The bass was loud."
    assert "x-kokoro-tone-guessed" not in response.headers


def test_a_marker_clears_the_ambiguity(ambiguous_gateway):
    client, server = ambiguous_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "The [sense:bass=music] was loud."},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "The base was loud."


def test_a_sense_tag_is_the_marker_syntax(ambiguous_gateway):
    """The tag grammar is the same one `[voice:]` and `[emotion:]` use."""
    client, server = ambiguous_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "A [sense:bass=fish] swam past."},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "A bas swam past."


def test_a_hint_clears_the_ambiguity(ambiguous_gateway):
    client, server = ambiguous_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "He plays bass in the band."},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "He plays base in the band."


def test_a_request_rule_clears_the_ambiguity(ambiguous_gateway):
    client, server = ambiguous_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={
            "input": "The bass was loud.",
            "pronunciations": {"bass": "base"},
        },
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "The base was loud."


def test_an_unknown_sense_is_rejected_not_spoken(ambiguous_gateway):
    client, server = ambiguous_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "The [sense:bass=jazz] was loud."},
    )

    assert response.status_code == 400
    assert "unknown sense tag" in response.json()["error"]["message"]
    assert server.requests == []


def test_a_malformed_sense_tag_is_rejected(ambiguous_gateway):
    client, _ = ambiguous_gateway
    response = client.post(
        "/v1/audio/speech", headers=AUTH, json={"input": "The [sense:bass] was loud."}
    )

    assert response.status_code == 400
    assert "invalid sense tag" in response.json()["error"]["message"]


@pytest.mark.parametrize("mode", ["shout", "error"])
def test_an_unknown_ambiguity_mode_is_rejected(ambiguous_gateway, mode):
    client, server = ambiguous_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "The bass was loud.", "ambiguity": mode},
    )

    assert response.status_code == 400
    assert "ambiguity must be 'guess' or 'ignore'" in response.json()["error"]["message"]
    assert server.requests == []


def test_a_literal_pipe_in_prose_is_not_a_marker(ambiguous_gateway):
    """Only a tag desugars to a marker; `a|b` in prose is just text."""
    client, server = ambiguous_gateway
    response = client.post("/v1/audio/speech", headers=AUTH, json={"input": "Use a|b for either."})

    assert response.status_code == 200
    assert server.last_request["input"] == "Use a|b for either."


def test_a_sense_tag_works_in_a_turn_array(ambiguous_gateway):
    client, server = ambiguous_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": [{"text": "A [sense:bass=fish] swam past."}]},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "A bas swam past."


def test_an_unknown_sense_in_a_turn_array_is_rejected(ambiguous_gateway):
    client, server = ambiguous_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": [{"text": "The [sense:bass=jazz] was loud."}]},
    )

    assert response.status_code == 400
    assert "unknown sense tag" in response.json()["error"]["message"]
    assert server.requests == []


# ---------------------------------------------------------------- overlay


def test_the_overlay_wins_over_the_default(overlay_gateway):
    client, server = overlay_gateway
    response = client.post("/v1/audio/speech", headers=AUTH, json={"input": "Ask Lachie."})

    assert response.status_code == 200
    assert server.last_request["input"] == "Ask Custom."


def test_the_default_still_applies_where_the_overlay_is_silent(overlay_gateway):
    client, server = overlay_gateway
    response = client.post("/v1/audio/speech", headers=AUTH, json={"input": "The bass was loud."})

    assert response.status_code == 200
    assert server.last_request["input"] == "The base was loud."


def test_a_request_rule_wins_over_the_overlay(overlay_gateway):
    client, server = overlay_gateway
    response = client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "Ask Lachie.", "pronunciations": {"Lachie": "Request"}},
    )

    assert response.status_code == 200
    assert server.last_request["input"] == "Ask Request."
