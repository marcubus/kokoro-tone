"""Number and acronym normalisation.

The digit table is the one the research-to-podcast skill already documents:
Australian English, years left as digits, and every rewrite bounded by a guard
so a version, a model name or a date is never mangled.
"""

import pytest

from kokoro_tone import normalise

SETTINGS = normalise.NormaliseSettings()


def style(locale="en-AU", max_words=64):
    return normalise.style_for(locale, max_words)


def numbers(text, locale="en-AU", max_words=64):
    active = style(locale, max_words)
    out, index = [], 0
    while index < len(text):
        hit = normalise.number_match(text, index, active)
        if hit is not None and hit[0] > index:
            out.append(hit[1])
            index = hit[0]
        else:
            out.append(text[index])
            index += 1
    return "".join(out)


def acronyms(text, table=None, letters=None, protect=()):
    letters = letters if letters is not None else normalise.letters_for("en-AU")
    active = frozenset(protect)
    out, index = [], 0
    while index < len(text):
        hit = normalise.acronym_match(text, index, table or {}, letters, active)
        if hit is not None and hit[0] > index:
            out.append(hit[1])
            index = hit[0]
        else:
            out.append(text[index])
            index += 1
    return "".join(out)


# ---------------------------------------------------------------- digits


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("3,913", "three thousand nine hundred and thirteen"),
        ("1,181", "one thousand one hundred and eighty-one"),
        ("105", "one hundred and five"),
        ("21", "twenty-one"),
        ("0", "zero"),
        ("0.43", "nought point four three"),
        ("3.14", "three point one four"),
        ("15%", "fifteen per cent"),
        ("0.5%", "nought point five per cent"),
        ("10-15", "ten to fifteen"),
        ("10-15%", "ten to fifteen per cent"),
        ("8-12", "eight to twelve"),
        ("21st", "twenty-first"),
        ("1st", "first"),
    ],
)
def test_digit_table(text, expected):
    assert numbers(text) == expected


def test_a_year_stays_as_digits():
    assert numbers("In 2026 the cost was 3913 dollars") == (
        "In 2026 the cost was three thousand nine hundred and thirteen dollars"
    )


def test_a_number_at_a_sentence_end_keeps_its_full_stop():
    assert numbers("It cost 15.") == "It cost fifteen."


@pytest.mark.parametrize(
    "text",
    [
        "MP3",
        "H264",
        "v2",
        "Kokoro-82M",
        "2.1.0",
        "192.0.2.1",
        "2026-11-11",
        "3:30",
        "11/11",
        "-5",
    ],
)
def test_guards_leave_compound_tokens_alone(text):
    assert numbers(text) == text


def test_max_words_leaves_a_long_expansion_as_digits():
    assert numbers("3913", max_words=2) == "3913"


def test_a_malformed_comma_group_is_not_rewritten():
    assert numbers("1,23") == "1,23"


# ---------------------------------------------------------------- locales


def test_american_locale_changes_the_three_audible_words():
    assert numbers("0.43", locale="en-US") == "zero point four three"
    assert numbers("15%", locale="en-US") == "fifteen percent"
    assert numbers("105", locale="en-US") == "one hundred five"


def test_letters_are_zed_in_australia_and_zee_in_america():
    assert normalise.letters_for("en-AU")["Z"] == "Zed"
    assert normalise.letters_for("en-GB")["Z"] == "Zed"
    assert normalise.letters_for("en-US")["Z"] == "Zee"


def test_an_unknown_locale_falls_back_to_australian(monkeypatch):
    monkeypatch.setenv("LOCALE", "fr-FR")
    assert normalise.load_normalise_settings().locale == "en-AU"


def test_the_two_normaliser_knobs_are_read_from_the_environment(monkeypatch):
    monkeypatch.setenv("LOCALE", "en-US")
    monkeypatch.setenv("NORMALISE_DEFAULT", "true")

    settings = normalise.load_normalise_settings()

    assert settings.locale == "en-US"
    assert settings.default_on is True


# ---------------------------------------------------------------- acronyms


def test_a_table_entry_wins():
    assert acronyms("SQL", table={"SQL": "sequel"}) == "sequel"


def test_an_unknown_token_is_spelled_letter_by_letter():
    assert acronyms("HTML") == "Aitch Tee Emm Ell"


def test_the_protect_list_is_honoured():
    assert acronyms("IT", protect=["IT"]) == "IT"


def test_a_single_letter_is_left_alone():
    assert acronyms("I") == "I"


def test_a_possessive_still_expands_the_token():
    assert acronyms("NASA's plan") == "En Aigh Ess Aigh's plan"


def test_a_token_inside_a_word_is_not_expanded():
    assert acronyms("MP3") == "MP3"


def test_a_token_longer_than_six_letters_is_left_alone():
    assert acronyms("ABCDEFG") == "ABCDEFG"


def test_american_letter_names_are_used_for_american_voices():
    assert acronyms("AZ", letters=normalise.letters_for("en-US")) == "Aigh Zee"
