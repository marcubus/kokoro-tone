"""The operator log: one line per event, counts and names only.

The gateway speaks whatever it is given, and the script may be private. These
tests pin both halves of that: the line is parseable, and the text never
appears in it.
"""

import json

from support import AUTH


def test_an_event_line_is_key_value(capsys):
    from kokoro_tone.log import event

    event("speech", status=200, turns=2, voice=None, guess="bass=music", detail="two words")

    line = capsys.readouterr().err.strip()
    assert line == 'event=speech status=200 turns=2 guess=bass=music detail="two words"'


def test_a_value_with_an_equals_sign_is_quoted(capsys):
    from kokoro_tone.log import event

    event("error", detail="speed must be between 0.75 and 1.3")

    assert 'detail="speed must be between 0.75 and 1.3"' in capsys.readouterr().err


def test_the_speech_line_carries_counts_not_the_script(gateway, capsys):
    client, _ = gateway

    client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "[pause:0.1s] Hello there.", "response_format": "mp3"},
    )

    err = capsys.readouterr().err
    line = [item for item in err.splitlines() if item.startswith("event=speech")][-1]
    assert "Hello there" not in err
    assert "turns=1" in line
    assert "chars=12" in line
    assert "fmt=mp3" in line
    assert "passthrough=no" in line
    assert "upstream_ms=" in line
    assert "render_ms=" in line


def test_a_passthrough_is_marked_as_one(gateway, capsys):
    client, _ = gateway

    client.post("/v1/audio/speech", headers=AUTH, json={"input": "Hello there."})

    err = capsys.readouterr().err
    line = [item for item in err.splitlines() if item.startswith("event=speech")][-1]
    assert "passthrough=yes" in line
    assert "Hello there" not in err


def test_the_guess_is_in_the_log_as_well_as_the_header(ambiguous_gateway, capsys):
    client, _ = ambiguous_gateway

    response = client.post("/v1/audio/speech", headers=AUTH, json={"input": "The bass was loud."})

    err = capsys.readouterr().err
    line = [item for item in err.splitlines() if item.startswith("event=speech")][-1]
    assert "guess=bass=music" in line
    assert response.headers["x-kokoro-tone-guessed"] == "bass=music"


def test_a_rejected_request_is_logged_with_its_reason(gateway, capsys):
    client, _ = gateway

    client.post(
        "/v1/audio/speech",
        headers=AUTH,
        json={"input": "hello", "ambiguity": "shout"},
    )

    err = capsys.readouterr().err
    line = [item for item in err.splitlines() if item.startswith("event=error")][-1]
    assert "status=400" in line
    assert "ambiguity must be" in line


def test_a_lexicon_load_is_logged(tmp_path, capsys):
    from kokoro_tone import pronounce
    from kokoro_tone.limits import DEFAULT_LIMITS

    path = tmp_path / "lexicon.json"
    path.write_text(
        json.dumps({"defaults": {"rules": [{"match": "Kubernetes", "say": "koober"}]}}),
        encoding="utf-8",
    )

    pronounce.Lexicon(str(path), DEFAULT_LIMITS).current()

    line = [item for item in capsys.readouterr().err.splitlines() if "event=lexicon" in item][-1]
    assert "rules=1" in line
    assert "overlay=no" in line
    assert "problems=0" in line


def test_an_overlay_is_named_in_the_lexicon_line(tmp_path, capsys):
    from kokoro_tone import pronounce
    from kokoro_tone.limits import DEFAULT_LIMITS

    base = tmp_path / "lexicon.json"
    base.write_text(
        json.dumps({"defaults": {"rules": [{"match": "Kubernetes", "say": "koober"}]}}),
        encoding="utf-8",
    )
    overlay = tmp_path / "custom.json"
    overlay.write_text(
        json.dumps({"defaults": {"rules": [{"match": "Grafana", "say": "grah-fah-na"}]}}),
        encoding="utf-8",
    )

    pronounce.Lexicon(str(base), DEFAULT_LIMITS, str(overlay)).current()

    line = [item for item in capsys.readouterr().err.splitlines() if "event=lexicon" in item][-1]
    assert "rules=2" in line
    assert "overlay=yes" in line
