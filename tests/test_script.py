"""The script layer as a standalone compiler, without HTTP or an engine."""

from __future__ import annotations

import io
import json

import pytest

from kokoro_tone import lexicon, script, script_cli
from kokoro_tone.errors import AmbiguityError, RequestError
from kokoro_tone.settings import load_settings


@pytest.fixture()
def settings():
    return load_settings()


@pytest.fixture()
def data(settings):
    return lexicon.load_lexicon("lexicon/lexicon.json", settings.limits)


def compile_text(text: str, settings, data, **kwargs) -> script.Compiled:
    return script.compile_script(
        script.ScriptInput(input=text, **kwargs),
        lexicon_data=data,
        settings=settings,
    )


def test_compile_text_needs_no_settings_or_lexicon():
    compiled = script.compile_text("Hello there.")

    assert compiled.text == "Hello there."
    assert compiled.voice == "af_heart"
    assert compiled.turns[0].text == "Hello there."


def test_compiled_exposes_a_json_ready_view():
    compiled = script.compile_text("[voice:am_michael] One.\n[voice:af_heart] Two.")
    payload = compiled.as_dict()

    assert payload["text"] == "One.\nTwo."
    assert [turn["voice"] for turn in payload["turns"]] == ["am_michael", "af_heart"]
    assert json.loads(compiled.to_json())["chars"] == compiled.chars


def test_compile_text_takes_a_lexicon_and_normalise_flag(data):
    compiled = script.compile_text("The bass was loud.", lexicon_data=data, normalise=True)

    assert compiled.text == "The base was loud."
    assert [item.chosen for item in compiled.found] == ["bass=music"]


def test_script_config_sets_defaults_without_the_environment():
    config = script.ScriptConfig(default_voice="bm_lewis")
    compiled = script.compile_text("Hello.", settings=config)

    assert compiled.voice == "bm_lewis"


def test_script_config_limits_are_enforced():
    from dataclasses import replace

    from kokoro_tone.limits import DEFAULT_LIMITS

    config = script.ScriptConfig(limits=replace(DEFAULT_LIMITS, input_chars=5))
    with pytest.raises(RequestError, match="input exceeds"):
        script.compile_text("too long", settings=config)


def test_heteronyms_are_resolved_and_guesses_are_reported(settings, data):
    compiled = compile_text("The bass was loud.", settings, data, normalise=True)

    assert compiled.turns[0].text == "The base was loud."
    assert [item.chosen for item in compiled.found] == ["bass=music"]


def test_ignore_leaves_the_word_alone_and_reports_nothing(settings, data):
    compiled = compile_text(
        "The bass was loud.", settings, data, normalise=True, ambiguity="ignore"
    )

    assert compiled.turns[0].text == "The bass was loud."
    assert compiled.found == ()


def test_markup_and_normalisation_work_without_a_request(settings, data):
    compiled = compile_text(
        "[voice:am_michael] [emotion:tired] HTML and 15%.",
        settings,
        data,
        normalise=True,
    )

    assert compiled.turns[0].voice == "am_michael"
    assert compiled.turns[0].emotion == "tired"
    assert compiled.turns[0].text == "Aitch Tee Emm Ell and fifteen per cent."


def test_an_unknown_ambiguity_mode_is_rejected(settings, data):
    with pytest.raises(RequestError, match="ambiguity must be"):
        compile_text("Hello.", settings, data, ambiguity="shout")


def test_error_mode_stops_with_pasteable_tags(settings, data):
    with pytest.raises(AmbiguityError) as caught:
        compile_text("The bass was loud.", settings, data, normalise=True, ambiguity="error")

    assert caught.value.ambiguities[0]["options"] == [
        "[sense:bass=music]",
        "[sense:bass=fish]",
    ]


def test_the_cli_emits_json(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("HTML."))
    code = script_cli.main(["--lexicon", "lexicon/lexicon.json", "--normalise", "--json"])

    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["turns"][0]["text"] == "Aitch Tee Emm Ell."


def test_the_cli_returns_three_for_an_invalid_script(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("[filter:nope] Hi."))
    code = script_cli.main(["--lexicon", "lexicon/lexicon.json"])

    assert code == 3
    assert "unknown filter block" in capsys.readouterr().err


def test_the_cli_returns_four_for_an_ambiguous_script(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", io.StringIO("The bass was loud."))
    code = script_cli.main(
        [
            "--lexicon",
            "lexicon/lexicon.json",
            "--normalise",
            "--ambiguity",
            "error",
        ]
    )

    assert code == 4
    assert "[sense:bass=music]" in capsys.readouterr().err


def test_the_cli_lists_filter_blocks(capsys):
    code = script_cli.main(["--list-filters"])

    assert code == 0
    blocks = {block["name"] for block in json.loads(capsys.readouterr().out)}
    assert {"high_shelf", "lowpass", "compressor"} <= blocks
