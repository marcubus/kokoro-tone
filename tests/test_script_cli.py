"""The offline compiler: the same answer as the server, with no server."""

import json

import pytest

from kokoro_tone import script_cli

LEXICON = {
    "defaults": {
        "rules": [
            {"match": "bass|music", "say": "base"},
            {"match": "bass|fish", "say": "bas"},
            {"match": "bass", "say": "base", "when": {"after": ["band", "music"]}},
            {"match": "bass", "say": "bas", "when": {"after": ["fish", "swam"]}},
            {"match": "Banyule", "say": "ban-yool"},
        ],
        "locale": "en-AU",
    },
    "voices": {},
    "aliases": {},
}

REQUEST = {
    "model": "kokoro-v1_0",
    "voice": "bm_lewis",
    "input": "The bass was loud.",
    "normalise": {"lexicon": True, "acronyms": True, "numbers": True},
    "pronunciations": {"Banyule": "ban-yool"},
    "ambiguity_guess": "guess",
}


@pytest.fixture()
def lexicon(tmp_path, monkeypatch):
    path = tmp_path / "lexicon.json"
    path.write_text(json.dumps(LEXICON), encoding="utf-8")
    monkeypatch.setenv("PRONUNCIATION_LEXICON", str(path))
    monkeypatch.setenv("PRONUNCIATION_OVERRIDES", str(tmp_path / "missing.json"))
    monkeypatch.setenv("PRONUNCIATION_LEARNED", str(tmp_path / "missing-learned.json"))
    monkeypatch.delenv("MAX_INPUT_CHARS", raising=False)
    return path


def _request(tmp_path, **overrides):
    doc = dict(REQUEST, **overrides)
    path = tmp_path / "request.json"
    path.write_text(json.dumps(doc), encoding="utf-8")
    return path


def test_request_mode_compiles_and_reports_the_guesses(lexicon, tmp_path, capsys):
    code = script_cli.main(["--request", str(_request(tmp_path))])

    assert code == 0
    body = json.loads(capsys.readouterr().out)
    assert body["text"] == "The base was loud."
    assert body["guesses"] == ["bass=music"]
    assert body["normalise"] == "lexicon+acronyms+numbers"
    assert body["voice"] == "bm_lewis"


def test_request_mode_uses_the_request_pronunciations(lexicon, tmp_path, capsys):
    path = _request(tmp_path, input="We visited Banyule.", pronunciations={"Banyule": "ban-yool"})

    assert script_cli.main(["--request", str(path)]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["text"] == "We visited Ban-yool."


def test_request_mode_rejects_an_unknown_sense_tag(lexicon, tmp_path, capsys):
    path = _request(tmp_path, input="The [sense:bass=jazz] was loud.")

    assert script_cli.main(["--request", str(path)]) == 3
    assert "unknown sense tag" in capsys.readouterr().err


def test_env_file_lifts_the_input_limit_the_deployment_raises(lexicon, tmp_path, capsys):
    """The code default is 4096; the deployed gateway accepts 60000."""
    long_input = "word " * 1200  # 6000 characters
    path = _request(tmp_path, input=long_input)

    assert script_cli.main(["--request", str(path)]) == 3
    assert "exceeds 4096" in capsys.readouterr().err

    env_file = tmp_path / "deployment.env"
    env_file.write_text("MAX_INPUT_CHARS=60000\nSECRET_TOKEN=do-not-load\n", encoding="utf-8")
    assert script_cli.main(["--request", str(path), "--env-file", str(env_file)]) == 0
    body = json.loads(capsys.readouterr().out)
    assert body["chars"] > 4096


def test_env_file_does_not_override_an_explicit_environment(lexicon, tmp_path, monkeypatch):
    monkeypatch.setenv("MAX_INPUT_CHARS", "5000")
    env_file = tmp_path / "deployment.env"
    env_file.write_text("MAX_INPUT_CHARS=60000\n", encoding="utf-8")

    path = _request(tmp_path, input="word " * 1200)
    assert script_cli.main(["--request", str(path), "--env-file", str(env_file)]) == 3


def test_learned_overlay_is_composed_like_the_server(lexicon, tmp_path, capsys):
    """A rule the gateway learned at runtime must be visible offline too."""
    learned = tmp_path / "learned.json"
    learned.write_text(
        json.dumps({"defaults": {"rules": [{"match": "Aurec", "say": "AW-rek"}]}}),
        encoding="utf-8",
    )
    path = _request(tmp_path, input="Aurec is hiring.")

    assert script_cli.main(["--request", str(path)]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "Aurec is hiring."

    assert script_cli.main(["--request", str(path), "--learned", str(learned)]) == 0
    assert json.loads(capsys.readouterr().out)["text"] == "AW-rek is hiring."
