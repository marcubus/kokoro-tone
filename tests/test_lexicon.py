"""The committed lexicon: one file, one rule list.

The file the gateway loads is the file you edit. These tests read it directly,
so a bad edit fails here before it reaches a request.
"""

import json
import pathlib

from kokoro_tone import pronounce
from kokoro_tone.limits import DEFAULT_LIMITS

LEXICON = pathlib.Path(__file__).resolve().parent.parent / "lexicon"
FILE = LEXICON / "lexicon.json"


def data():
    return pronounce.load_lexicon(str(FILE), DEFAULT_LIMITS)


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
