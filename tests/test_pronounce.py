"""Pronunciation rules, per-voice sets, server aliases and precedence.

The scanner is one left-to-right pass: a replacement is never re-scanned, and
a rule never sees tag values because the text is rewritten after parsing.
"""

import json
import os

import pytest

from kokoro_tone import normalise, pronounce
from kokoro_tone.limits import DEFAULT_LIMITS
from kokoro_tone.parse import MarkupError

SETTINGS = normalise.NormaliseSettings()
NO_FLAGS = pronounce.Flags()


def rules(value):
    return pronounce.parse_rules(value, DEFAULT_LIMITS)


def rewrite(
    text,
    *,
    token=None,
    voice=None,
    global_rules=(),
    alias=None,
    lexicon=pronounce.EMPTY_LEXICON,
    flags=NO_FLAGS,
    settings=SETTINGS,
):
    effective = pronounce.effective_set(token, voice, global_rules, alias or {}, lexicon, settings)
    return pronounce.build_rewriter(effective, flags, settings, DEFAULT_LIMITS).apply(text)


def set_of(**kwargs):
    return pronounce.RuleSet(**kwargs)


def lexicon_of(**kwargs):
    return pronounce.LexiconData(**kwargs)


# ---------------------------------------------------------------- rule shape


def test_map_shorthand_is_a_list_of_literal_rules():
    parsed = rules({"AWS": "Aigh Double You Ess", "bass": "base"})
    assert [rule.match for rule in parsed] == ["AWS", "bass"]
    assert [rule.say for rule in parsed] == ["Aigh Double You Ess", "base"]


def test_a_rule_can_delete_a_word():
    assert rewrite("Well um, yes", global_rules=rules({"um, ": ""})) == "Well yes"


def test_a_rule_can_be_case_sensitive():
    parsed = rules([{"match": "US", "say": "You Ess", "case_sensitive": True}])
    assert rewrite("US us", global_rules=parsed) == "You Ess us"


def test_matching_is_case_insensitive_by_default():
    assert rewrite("Bass", global_rules=rules({"bass": "base"})) == "Base"
    assert rewrite("BASS", global_rules=rules({"bass": "base"})) == "Base"


def test_whole_word_is_the_default():
    parsed = rules({"AI": "Aigh Eye"})
    assert rewrite("AI AIM", global_rules=parsed) == "Aigh Eye AIM"


def test_whole_word_can_be_disabled():
    parsed = rules([{"match": "AI", "say": "Aigh Eye", "whole_word": False}])
    assert rewrite("AIM", global_rules=parsed) == "Aigh EyeM"


def test_the_longest_match_wins():
    parsed = rules({"York": "Y", "New York": "NYC"})
    assert rewrite("New York", global_rules=parsed) == "NYC"


def test_a_replacement_is_never_reprocessed():
    parsed = rules({"bass": "base", "base": "B"})
    assert rewrite("bass", global_rules=parsed) == "base"


def test_a_rule_can_require_a_word_after_it():
    parsed = rules(
        [
            {"match": "bass", "say": "base", "when": {"after": ["band", "music"]}},
            {"match": "bass", "say": "bass"},
        ]
    )
    assert rewrite("He plays bass in the band.", global_rules=parsed) == (
        "He plays base in the band."
    )
    assert rewrite("The bass swam away.", global_rules=parsed) == "The bass swam away."


def test_a_rule_can_require_a_word_before_it():
    parsed = rules([{"match": "read", "say": "red", "when": {"before": ["have", "had"]}}])
    assert rewrite("I have read it.", global_rules=parsed) == "I have red it."
    assert rewrite("I read it.", global_rules=parsed) == "I read it."


def test_the_context_window_is_bounded():
    parsed = rules([{"match": "bass", "say": "base", "when": {"after": ["band"], "within": 3}}])
    assert rewrite("bass one two band", global_rules=parsed) == "base one two band"
    assert rewrite("bass one two three band", global_rules=parsed) == "bass one two three band"


@pytest.mark.parametrize(
    "value",
    [
        {"bass": "ba[ss]"},
        [{"match": "bass", "say": "base", "extra": 1}],
        [{"match": "bass", "say": "base", "when": {"within": 0}}],
        [{"match": "bass", "say": "base", "when": {"after": ["a"] * 17}}],
        [{"match": "", "say": "base"}],
        [{"match": "bass", "say": "base", "case_sensitive": "yes"}],
    ],
)
def test_invalid_rules_are_rejected(value):
    with pytest.raises(MarkupError):
        rules(value)


def test_the_rule_cap_is_enforced():
    value = {f"word{index}": "x" for index in range(257)}
    with pytest.raises(MarkupError):
        rules(value)


# ---------------------------------------------------------------- request sets


def test_a_per_alias_map_keeps_the_global_part_under_star():
    global_rules, per_alias = pronounce.parse_request_sets(
        {"*": {"Lachie": "Locky"}, "narrator": [{"match": "A", "say": "Aigh"}]},
        DEFAULT_LIMITS,
    )
    assert [rule.match for rule in global_rules] == ["Lachie"]
    assert [rule.match for rule in per_alias["narrator"]] == ["A"]


def test_an_all_string_map_is_the_global_shorthand():
    global_rules, per_alias = pronounce.parse_request_sets({"bass": "base"}, DEFAULT_LIMITS)
    assert [rule.match for rule in global_rules] == ["bass"]
    assert per_alias == {}


def test_per_alias_values_must_be_lists():
    with pytest.raises(MarkupError):
        pronounce.parse_request_sets({"narrator": {"match": "A", "say": "Ay"}}, DEFAULT_LIMITS)


def test_per_alias_keys_use_the_voice_grammar():
    with pytest.raises(MarkupError):
        pronounce.parse_request_sets({"bad alias": []}, DEFAULT_LIMITS)


# ---------------------------------------------------------------- precedence


def test_a_request_alias_rule_beats_everything():
    lexicon = lexicon_of(
        defaults=set_of(rules=rules({"word": "default"})),
        voices={"bm_lewis": set_of(rules=rules({"word": "voice"}))},
    )
    result = rewrite(
        "word",
        token="narrator",
        voice="bm_lewis",
        global_rules=rules({"word": "global"}),
        alias={"narrator": rules({"word": "alias"})},
        lexicon=lexicon,
        flags=pronounce.Flags(lexicon=True),
    )
    assert result == "alias"


def test_a_deployment_voice_rule_beats_a_request_global_rule():
    lexicon = lexicon_of(
        defaults=set_of(rules=rules({"word": "default"})),
        voices={"bm_lewis": set_of(rules=rules({"word": "voice"}))},
    )
    result = rewrite(
        "word",
        token="narrator",
        voice="bm_lewis",
        global_rules=rules({"word": "global"}),
        lexicon=lexicon,
        flags=pronounce.Flags(lexicon=True),
    )
    assert result == "voice"


def test_a_request_global_rule_beats_deployment_defaults():
    lexicon = lexicon_of(defaults=set_of(rules=rules({"word": "default"})))
    result = rewrite(
        "word",
        global_rules=rules({"word": "global"}),
        lexicon=lexicon,
        flags=pronounce.Flags(lexicon=True),
    )
    assert result == "global"


def test_a_resolved_voice_set_applies_through_an_alias():
    lexicon = lexicon_of(voices={"bm_lewis": set_of(rules=rules({"word": "voice"}))})
    result = rewrite(
        "word",
        token="narrator",
        voice="bm_lewis",
        lexicon=lexicon,
        flags=pronounce.Flags(lexicon=True),
    )
    assert result == "voice"


def test_a_per_voice_locale_and_letters_are_used():
    lexicon = lexicon_of(voices={"am_michael": set_of(locale="en-US")})
    effective = pronounce.effective_set(None, "am_michael", (), {}, lexicon, SETTINGS)
    assert effective.locale == "en-US"
    assert effective.letters["Z"] == "Zee"


def test_per_voice_letters_override_the_locale_table():
    lexicon = lexicon_of(voices={"bm_lewis": set_of(letters={"A": "Ay"})})
    effective = pronounce.effective_set(None, "bm_lewis", (), {}, lexicon, SETTINGS)
    assert effective.letters["A"] == "Ay"
    assert effective.letters["Z"] == "Zed"


# ---------------------------------------------------------------- gating


def test_request_rules_apply_without_normalise():
    assert rewrite("bass", global_rules=rules({"bass": "base"})) == "base"


def test_deployment_rules_need_the_lexicon_layer():
    lexicon = lexicon_of(defaults=set_of(rules=rules({"bass": "base"})))
    assert rewrite("bass", lexicon=lexicon) == "bass"
    assert rewrite("bass", lexicon=lexicon, flags=pronounce.Flags(lexicon=True)) == "base"


def test_acronyms_need_the_acronym_layer():
    lexicon = lexicon_of(defaults=set_of(acronyms={"SQL": "sequel"}))
    assert rewrite("SQL", lexicon=lexicon) == "SQL"
    assert rewrite("SQL", lexicon=lexicon, flags=pronounce.Flags(acronyms=True)) == "sequel"


def test_numbers_need_the_numbers_layer():
    assert rewrite("15%") == "15%"
    assert rewrite("15%", flags=pronounce.Flags(numbers=True)) == "fifteen per cent"


def test_flags_parse_true_false_and_layers():
    assert pronounce.parse_flags(None, SETTINGS) == pronounce.Flags()
    assert pronounce.parse_flags(True, SETTINGS) == pronounce.Flags(True, True, True)
    assert pronounce.parse_flags(False, SETTINGS) == pronounce.Flags()
    assert pronounce.parse_flags({"numbers": True}, SETTINGS) == pronounce.Flags(numbers=True)


def test_normalise_default_from_the_environment(monkeypatch):
    settings = normalise.NormaliseSettings(default_on=True, acronyms=False)
    assert pronounce.parse_flags(None, settings) == pronounce.Flags(
        lexicon=True, acronyms=False, numbers=True
    )


def test_unknown_normalise_keys_are_rejected():
    with pytest.raises(MarkupError):
        pronounce.parse_flags({"numbers": True, "extra": True}, SETTINGS)


# ---------------------------------------------------------------- lexicon file


def write_lexicon(tmp_path, data):
    path = tmp_path / "lexicon.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_the_lexicon_file_loads_sets_and_aliases(tmp_path):
    path = write_lexicon(
        tmp_path,
        {
            "defaults": {"rules": {"Lachie": "Locky"}},
            "voices": {"bm_lewis": {"letters": {"A": "Aigh"}, "locale": "en-GB"}},
            "aliases": {"id": "bm_lewis", "narrator": "bm_lewis"},
        },
    )
    data = pronounce.load_lexicon(path, DEFAULT_LIMITS)

    assert data.problems == ()
    assert data.defaults.rules[0].say == "Locky"
    assert data.voices["bm_lewis"].letters == {"A": "Aigh"}
    assert data.aliases == {"narrator": "bm_lewis"}
    assert data.default_alias == "bm_lewis"


def test_a_malicious_alias_name_is_skipped_not_loaded(tmp_path):
    path = write_lexicon(
        tmp_path,
        {"aliases": {"narrator": "af_bella", "bad alias": "af_bella", "$(id)": "af_bella"}},
    )
    data = pronounce.load_lexicon(path, DEFAULT_LIMITS)

    assert data.aliases == {"narrator": "af_bella"}
    assert len(data.problems) == 2


def test_a_cyclic_alias_is_dropped(tmp_path):
    path = write_lexicon(tmp_path, {"aliases": {"a": "b", "b": "a"}})
    data = pronounce.load_lexicon(path, DEFAULT_LIMITS)

    assert data.aliases == {}
    assert any("cycle" in problem for problem in data.problems)


def test_an_oversized_lexicon_is_ignored(tmp_path):
    from dataclasses import replace

    path = write_lexicon(tmp_path, {"defaults": {"rules": {"a": "b"}}})
    data = pronounce.load_lexicon(path, replace(DEFAULT_LIMITS, lexicon_bytes=8))

    assert data.defaults.rules == ()
    assert any("exceeds" in problem for problem in data.problems)


def test_a_malformed_set_is_skipped_and_the_rest_loads(tmp_path):
    path = write_lexicon(
        tmp_path,
        {
            "defaults": {"rules": {"Lachie": "Locky"}, "locale": "fr-FR"},
            "voices": {"bm_lewis": {"letters": {"A": "Aigh"}}},
        },
    )
    data = pronounce.load_lexicon(path, DEFAULT_LIMITS)

    assert data.defaults.rules[0].match == "Lachie"
    assert data.defaults.locale is None
    assert data.voices["bm_lewis"].letters == {"A": "Aigh"}


def test_the_lexicon_reloads_when_the_file_changes(tmp_path):
    path = write_lexicon(tmp_path, {"defaults": {"rules": {"bass": "base"}}})
    lexicon = pronounce.Lexicon(path, DEFAULT_LIMITS)

    assert lexicon.current().defaults.rules[0].say == "base"

    path = write_lexicon(tmp_path, {"defaults": {"rules": {"bass": "BAYSS"}}})
    os.utime(path, ns=(1, 1))
    assert lexicon.current().defaults.rules[0].say == "BAYSS"


def test_a_missing_lexicon_file_is_empty_not_fatal(tmp_path):
    lexicon = pronounce.Lexicon(str(tmp_path / "absent.json"), DEFAULT_LIMITS)
    assert lexicon.current() == pronounce.EMPTY_LEXICON


def test_a_bad_rule_is_skipped_and_the_rest_of_the_set_loads(tmp_path):
    path = write_lexicon(
        tmp_path,
        {
            "defaults": {
                "rules": [
                    {"match": "good", "say": "ok"},
                    {"match": "bad", "say": "ba[d]"},
                    {"match": "also", "say": "fine"},
                ]
            }
        },
    )
    data = pronounce.load_lexicon(path, DEFAULT_LIMITS)

    assert [rule.match for rule in data.defaults.rules] == ["good", "also"]
    assert any("rules" in problem for problem in data.problems)


def test_a_sense_marker_selects_a_respelling():
    parsed = rules(
        [
            {"match": "bass|music", "say": "base"},
            {"match": "bass|fish", "say": "bas"},
        ]
    )
    assert rewrite("He plays bass|music.", global_rules=parsed) == "He plays base."
    assert rewrite("A bass|fish swam.", global_rules=parsed) == "A bas swam."


def test_an_unknown_sense_marker_is_left_as_text():
    parsed = rules([{"match": "bass|music", "say": "base"}])
    assert rewrite("bass|jazz", global_rules=parsed) == "bass|jazz"


def test_a_replacement_keeps_a_sentence_initial_capital():
    parsed = rules({"wind": "wined"})
    assert rewrite("Wind the clock", global_rules=parsed) == "Wined the clock"
    assert rewrite("wind the clock", global_rules=parsed) == "wined the clock"


def test_the_first_rule_whose_context_is_satisfied_wins():
    parsed = rules(
        [
            {"match": "row", "say": "roh", "when": {"after": ["boat"]}},
            {"match": "row", "say": "rau", "when": {"after": ["seats"]}},
        ]
    )
    assert rewrite("row boat", global_rules=parsed) == "roh boat"
    assert rewrite("row seats", global_rules=parsed) == "rau seats"


def test_a_rule_whose_context_fails_lets_a_later_rule_apply():
    parsed = rules(
        [
            {"match": "row", "say": "roh", "when": {"after": ["boat"]}},
            {"match": "row", "say": "rau"},
        ]
    )
    assert rewrite("row seats", global_rules=parsed) == "rau seats"


def test_a_bare_word_with_no_matching_context_is_left_alone():
    parsed = rules([{"match": "row", "say": "rau", "when": {"after": ["seats"]}}])
    assert rewrite("row", global_rules=parsed) == "row"


def test_before_and_after_in_one_rule_must_both_match():
    """Why the lexicon emits one rule per side: `when` is AND across lists."""
    parsed = rules(
        [{"match": "row", "say": "rau", "when": {"before": ["the"], "after": ["seats"]}}]
    )
    assert rewrite("the row seats", global_rules=parsed) == "the rau seats"
    assert rewrite("the row", global_rules=parsed) == "the row"


def test_a_before_cue_is_local_by_default():
    parsed = rules([{"match": "record", "say": "verb", "when": {"before": ["they"]}}])
    assert rewrite("They record it", global_rules=parsed) == "They verb it"
    # "they" is three words back, and a pronoun that far away belongs to the
    # clause, not to the word: "I think the record" is a noun.
    assert rewrite("They said the record failed", global_rules=parsed) == (
        "They said the record failed"
    )


def test_an_after_cue_reaches_the_end_of_the_sentence():
    parsed = rules([{"match": "bass", "say": "base", "when": {"after": ["band"]}}])
    text = "bass one two three four five six seven eight nine band"
    assert rewrite(text, global_rules=parsed) == text.replace("bass", "base")


def test_a_cue_in_another_sentence_does_not_count():
    parsed = rules([{"match": "bass", "say": "base", "when": {"after": ["band"]}}])
    assert rewrite("bass. The band played.", global_rules=parsed) == "bass. The band played."
    assert rewrite("bass! The band.", global_rules=parsed) == "bass! The band."
    assert rewrite("bass\nThe band", global_rules=parsed) == "bass\nThe band"


def test_an_explicit_window_overrides_the_default():
    parsed = rules([{"match": "record", "say": "verb", "when": {"before": ["they"], "within": 5}}])
    assert rewrite("They said the record failed", global_rules=parsed) == (
        "They said the verb failed"
    )


# ---------------------------------------------------------------- ambiguity


def report(text, **kwargs):
    effective = pronounce.effective_set(
        kwargs.get("token"),
        kwargs.get("voice"),
        kwargs.get("global_rules", ()),
        kwargs.get("alias") or {},
        kwargs.get("lexicon", pronounce.EMPTY_LEXICON),
        SETTINGS,
    )
    flags = kwargs.get("flags", pronounce.Flags(lexicon=True))
    rewriter = pronounce.build_rewriter(effective, flags, SETTINGS, DEFAULT_LIMITS)
    found: list[pronounce.Ambiguity] = []
    return rewriter.apply(text, ambiguities=found, guess=kwargs.get("guess", False)), found


def heteronym_lexicon():
    return lexicon_of(
        defaults=set_of(
            rules=rules(
                [
                    {"match": "bass|music", "say": "base"},
                    {"match": "bass|fish", "say": "bas"},
                    {"match": "bass", "say": "base", "when": {"after": ["band"]}},
                    {"match": "bass", "say": "bas", "when": {"after": ["fish"]}},
                ]
            )
        )
    )


def test_an_unsettled_heteronym_is_reported_with_the_rule_order():
    """The ordered rules always decide: the first candidate is the answer."""
    out, found = report("The bass was loud.", lexicon=heteronym_lexicon())

    assert out == "The bass was loud."
    assert len(found) == 1
    assert found[0].word == "bass"
    assert found[0].offset == 4
    assert found[0].say == "base"
    assert found[0].chosen == "bass=music"


def test_a_guess_substitutes_the_first_candidate():
    out, found = report("The bass was loud.", lexicon=heteronym_lexicon(), guess=True)

    assert out == "The base was loud."
    assert [item.chosen for item in found] == ["bass=music"]


def test_a_guess_keeps_a_sentence_initial_capital():
    out, _ = report("Bass was loud.", lexicon=heteronym_lexicon(), guess=True)
    assert out == "Base was loud."


def test_a_guess_is_not_made_when_the_word_is_settled():
    out, found = report("He plays bass in the band.", lexicon=heteronym_lexicon(), guess=True)
    assert out == "He plays base in the band."
    assert found == []


def test_a_hint_settles_the_word_so_it_is_not_reported():
    out, found = report("He plays bass in the band.", lexicon=heteronym_lexicon())
    assert out == "He plays base in the band."
    assert found == []


def test_a_marker_settles_the_word_so_it_is_not_reported():
    out, found = report("The bass|fish was loud.", lexicon=heteronym_lexicon())
    assert out == "The bas was loud."
    assert found == []


def test_a_word_with_one_decision_rule_is_not_a_heteronym():
    lexicon = lexicon_of(
        defaults=set_of(
            rules=rules([{"match": "bass", "say": "base", "when": {"after": ["band"]}}])
        )
    )
    _, found = report("The bass was loud.", lexicon=lexicon)
    assert found == []


def test_every_occurrence_is_reported_with_its_own_offset():
    _, found = report("bass and bass", lexicon=heteronym_lexicon())
    assert [item.offset for item in found] == [0, 9]


def test_an_ambiguity_inside_a_longer_match_is_not_reported():
    lexicon = lexicon_of(
        defaults=set_of(
            rules=rules(
                [
                    {"match": "bass|music", "say": "base"},
                    {"match": "bass|fish", "say": "bas"},
                    {"match": "bass", "say": "base", "when": {"after": ["band"]}},
                    {"match": "bass", "say": "bas", "when": {"after": ["fish"]}},
                    {"match": "bass line", "say": "bassline"},
                ]
            )
        )
    )
    out, found = report("the bass line", lexicon=lexicon)
    assert out == "the bassline"
    assert found == []


def test_a_guess_never_rewrites_inside_a_marker():
    """`bass|jazz` is a sense the lexicon lacks: fail, do not guess over it."""
    out, found = report("The bass|jazz was loud.", lexicon=heteronym_lexicon(), guess=True)
    assert out == "The bass|jazz was loud."
    assert found == []


def test_a_guess_and_its_header_agree_when_markers_are_ordered_differently():
    """`chosen` names the tag that produces the guessed respelling, not marker[0]."""
    lexicon = lexicon_of(
        defaults=set_of(
            rules=rules(
                [
                    {"match": "bass|fish", "say": "bas"},
                    {"match": "bass|music", "say": "base"},
                    {"match": "bass", "say": "base", "when": {"after": ["band"]}},
                    {"match": "bass", "say": "bas", "when": {"after": ["fish"]}},
                ]
            )
        )
    )

    out, found = report("The bass was loud.", lexicon=lexicon, guess=True)

    assert out == "The base was loud."
    assert found[0].chosen == "bass=music"


# ---------------------------------------------------------------- composition


def test_an_overlay_rule_is_tried_before_the_default():
    base = lexicon_of(defaults=set_of(rules=rules([{"match": "Lachie", "say": "default"}])))
    overlay = lexicon_of(defaults=set_of(rules=rules([{"match": "Lachie", "say": "custom"}])))

    merged = pronounce.compose(base, overlay)

    assert [rule.say for rule in merged.defaults.rules] == ["custom", "default"]


def test_an_overlay_scalar_wins():
    base = lexicon_of(
        defaults=set_of(acronyms={"SQL": "sequel"}, letters={"A": "Ay"}, locale="en-AU")
    )
    overlay = lexicon_of(
        defaults=set_of(acronyms={"SQL": "ess cue ell"}, letters={"A": "Aigh"}, locale="en-GB")
    )

    merged = pronounce.compose(base, overlay)

    assert merged.defaults.acronyms["SQL"] == "ess cue ell"
    assert merged.defaults.letters["A"] == "Aigh"
    assert merged.defaults.locale == "en-GB"


def test_an_overlay_voice_set_composes_with_the_default():
    base = lexicon_of(voices={"bm_lewis": set_of(rules=rules([{"match": "a", "say": "ay"}]))})
    overlay = lexicon_of(voices={"bm_lewis": set_of(rules=rules([{"match": "a", "say": "Aigh"}]))})

    merged = pronounce.compose(base, overlay)

    assert [rule.say for rule in merged.voices["bm_lewis"].rules] == ["Aigh", "ay"]


def test_an_overlay_alias_and_default_voice_win():
    base = lexicon_of(aliases={"narrator": "am_michael"}, default_alias="am_michael")
    overlay = lexicon_of(aliases={"narrator": "bm_lewis"}, default_alias="bm_lewis")

    merged = pronounce.compose(base, overlay)

    assert merged.aliases["narrator"] == "bm_lewis"
    assert merged.default_alias == "bm_lewis"


def test_a_request_rule_still_beats_the_overlay():
    base = lexicon_of(defaults=set_of(rules=rules([{"match": "bass", "say": "default"}])))
    overlay = lexicon_of(defaults=set_of(rules=rules([{"match": "bass", "say": "custom"}])))
    merged = pronounce.compose(base, overlay)

    out = rewrite("bass", global_rules=rules([{"match": "bass", "say": "request"}]), lexicon=merged)

    assert out == "request"


def test_the_composed_lexicon_reloads_when_the_overlay_changes(tmp_path):
    base_path = tmp_path / "lexicon.json"
    overlay_path = tmp_path / "custom.json"
    base_path.write_text(
        json.dumps({"defaults": {"rules": [{"match": "bass", "say": "default"}]}}),
        encoding="utf-8",
    )
    overlay_path.write_text(
        json.dumps({"defaults": {"rules": [{"match": "bass", "say": "custom"}]}}),
        encoding="utf-8",
    )
    source = pronounce.Lexicon(str(base_path), DEFAULT_LIMITS, str(overlay_path))

    assert source.current().defaults.rules[0].say == "custom"

    overlay_path.write_text(
        json.dumps({"defaults": {"rules": [{"match": "bass", "say": "new"}]}}),
        encoding="utf-8",
    )
    assert source.current().defaults.rules[0].say == "new"


def test_a_missing_overlay_is_not_a_problem(tmp_path):
    base_path = tmp_path / "lexicon.json"
    base_path.write_text(
        json.dumps({"defaults": {"rules": [{"match": "bass", "say": "base"}]}}),
        encoding="utf-8",
    )
    source = pronounce.Lexicon(str(base_path), DEFAULT_LIMITS, str(tmp_path / "missing.json"))

    data = source.current()

    assert data.problems == ()
    assert data.defaults.rules[0].say == "base"
