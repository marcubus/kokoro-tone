"""The committed lexicon: one file, one rule list.

The file the gateway loads is the file you edit. These tests read it directly,
so a bad edit fails here before it reaches a request.
"""

import json
import pathlib

from kokoro_tone import lexicon, pronounce, script
from kokoro_tone.limits import DEFAULT_LIMITS
from kokoro_tone.normalise import NormaliseSettings
from kokoro_tone.settings import load_settings

LEXICON = pathlib.Path(__file__).resolve().parent.parent / "lexicon"
FILE = LEXICON / "lexicon.json"


def data():
    return lexicon.load_lexicon(str(FILE), DEFAULT_LIMITS)


def rules():
    return data().defaults.rules


def markers():
    return [rule for rule in rules() if "|" in rule.match]


def bare():
    return [rule for rule in rules() if "|" not in rule.match and (rule.before or rule.after)]


def test_the_file_loads_without_problems():
    assert data().problems == ()


def test_the_file_is_a_lexicon_set():
    raw = json.loads(FILE.read_text(encoding="utf-8"))
    assert set(raw) == {"defaults", "voices", "aliases"}
    assert set(raw["defaults"]) <= {"rules", "acronyms", "protect", "letters", "locale"}


def test_the_rule_count_is_within_the_cap():
    assert 0 < len(rules()) <= DEFAULT_LIMITS.lexicon_rules


def test_every_heteronym_has_two_unique_tags():
    tags = [rule.match for rule in markers()]
    assert len(tags) == len(set(tags)) == 200
    words = [tag.split("|", 1)[0] for tag in tags]
    assert len(set(words)) == 100
    for word in set(words):
        assert words.count(word) == 2


def test_the_colliding_labels_have_readable_tags():
    """`bow` and `sewer` name their senses, not two `noun` keys."""
    tags = {rule.match for rule in markers()}
    assert {"bow|ribbon", "bow|ship"} <= tags
    assert {"sewer|one-who-sews", "sewer|the-drain"} <= tags


def test_a_stress_word_is_marker_only():
    """No respelling can split a stress pair, so there is no bare rule."""
    words = {rule.match.split("|", 1)[0] for rule in markers()}
    decided = {rule.match for rule in bare()}
    assert "digest" in words
    assert "digest" not in decided


def test_the_bare_rows_are_decidable():
    """`bare` means the literal spelling is claimed, so hints are mandatory."""
    decided = {rule.match for rule in bare()}
    assert {"bass", "does"} <= decided


def test_a_bare_rule_leaves_the_window_to_the_gateway():
    """The sentence is the default, so the data carries no window at all."""
    assert bare()
    assert all(rule.within is None for rule in bare())


def test_the_file_carries_the_deployment_tables():
    defaults = data().defaults
    assert defaults.locale == "en-AU"
    assert defaults.acronyms
    assert defaults.letters
    assert "END" in defaults.protect


def test_the_morning_glory_heteronyms_are_decided():
    """The committed lexicon decides the words that were guessed wrong in the
    Morning Glory render: record after finance, separate after a number, live
    before a noun, and Terraform's stress."""
    settings = NormaliseSettings()
    effective = pronounce.effective_set(None, None, (), {}, data(), settings)
    rewriter = pronounce.build_rewriter(
        effective, pronounce.Flags(lexicon=True), settings, DEFAULT_LIMITS
    )
    text = (
        "the regulated finance record against it. "
        "eleven separate vulnerabilities. "
        "It is Terraform at scale. "
        "a live tool."
    )
    out = rewriter.apply(text).lower()
    assert "rek-awd" in out
    assert "eleven separate vulnerabilities" in out
    assert "ter-uh-form" in out
    assert "lyve tool" in out
    assert "rih-kawd" not in out
    assert "sep-uh-rut" not in out
    assert "sep-uh-rayt" not in out
    assert "tare-ah-form" not in out
    assert "liv tool" not in out


def test_separate_respellings_leave_the_engine_its_own_stress():
    """`sep-uh-rut` phonemises as sˈɛpˈʌɹˈʌt: a stress on every syllable and the
    wrong vowel. The adjective is left as the plain word, which the engine reads
    as sˈɛpəɹət; only the verb needs a respelling, and `separayt` gives the
    engine sˈɛpəɹˌAt."""
    settings = load_settings()
    text = (
        "The card carries no [sense:separate=adj] minimum. We need to [sense:separate=verb] them."
    )
    compiled = script.compile_script(
        script.ScriptInput(input=text, normalise=True),
        lexicon_data=data(),
        settings=settings,
    )
    out = compiled.text
    assert out == "The card carries no separate minimum. We need to separayt them."
    assert "sep-uh-rut" not in out
    assert "sep-uh-rayt" not in out


# ------------------------------------------------ the learned overlay limits


def _learned_lexicon(tmp_path, **limits):
    from dataclasses import replace

    from kokoro_tone.lexicon import Lexicon

    path = tmp_path / "learned" / "learned.json"
    return Lexicon(
        str(FILE),
        replace(DEFAULT_LIMITS, **limits),
        learned_path=str(path),
        write_enabled=True,
    ), path


def test_learned_overlay_is_capped_at_the_rule_limit(tmp_path):
    import pytest

    from kokoro_tone.parse import MarkupError

    lexicon_object, path = _learned_lexicon(tmp_path, lexicon_rules=2)
    lexicon_object.learn("One", "one")
    lexicon_object.learn("Two", "two")

    with pytest.raises(MarkupError, match="exceed 2 rules"):
        lexicon_object.learn("Three", "three")

    stored = json.loads(path.read_text(encoding="utf-8"))
    assert len(stored["defaults"]["rules"]) == 2


def test_learned_overlay_is_capped_at_the_byte_limit(tmp_path):
    import pytest

    from kokoro_tone.parse import MarkupError

    lexicon_object, _ = _learned_lexicon(tmp_path, lexicon_bytes=80)

    with pytest.raises(MarkupError, match="exceed 80 bytes"):
        lexicon_object.learn("Aurec", "AW-rek")


def test_learned_overlay_refuses_a_symlink(tmp_path):
    import pytest

    from kokoro_tone.parse import MarkupError

    lexicon_object, path = _learned_lexicon(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    target = tmp_path / "elsewhere.json"
    target.write_text("{}", encoding="utf-8")
    path.symlink_to(target)

    with pytest.raises(MarkupError, match="symlink"):
        lexicon_object.learn("Aurec", "AW-rek")
    assert target.read_text(encoding="utf-8") == "{}"


def test_lookup_reports_a_per_voice_rule(tmp_path):
    path = tmp_path / "lexicon.json"
    path.write_text(
        json.dumps(
            {
                "defaults": {"rules": [{"match": "read|present-verb", "say": "reed"}]},
                "voices": {
                    "bm_lewis": {"rules": [{"match": "read|past-verb", "say": "red"}]},
                },
            }
        ),
        encoding="utf-8",
    )

    found = lexicon.Lexicon(str(path), DEFAULT_LIMITS).lookup("read")

    assert found["found"] is True
    assert [entry.get("voice") for entry in found["rules"]] == [None, "bm_lewis"]
    assert found["senses"] == [
        {"sense": "present-verb", "say": "reed", "source": "defaults"},
        {"sense": "past-verb", "say": "red", "source": "defaults", "voice": "bm_lewis"},
    ]


def test_learned_overlay_rejects_a_corrupt_file(tmp_path):
    import pytest

    from kokoro_tone.parse import MarkupError

    lexicon_object, path = _learned_lexicon(tmp_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("not json", encoding="utf-8")

    with pytest.raises(MarkupError, match="not readable"):
        lexicon_object.learn("Aurec", "AW-rek")
    assert path.read_text(encoding="utf-8") == "not json"
