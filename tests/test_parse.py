import pytest

from kokoro_tone import parse


def test_three_voices_split_into_turns():
    text = (
        "[voice:af_heart] [emotion:warm] The rain had stopped by morning. "
        "[voice:am_michael] [emotion:tired] Did anyone check the gauge? "
        "[pause:1.2s] "
        "[voice:bf_emma] I did - it's over the wall."
    )
    turns = parse.parse_markup(text, "af_bella")

    assert [t.voice for t in turns] == ["af_heart", "am_michael", "bf_emma"]
    # Tags are sticky: bf_emma keeps the `tired` preset until it is changed.
    assert [t.emotion for t in turns] == ["warm", "tired", "tired"]
    assert turns[0].text == "The rain had stopped by morning."
    assert turns[1].gap_ms == 1200
    assert turns[2].text.startswith("I did")


def test_default_voice_applies_to_untagged_text():
    turns = parse.parse_markup("Just one line.", "af_bella")
    assert len(turns) == 1
    assert turns[0].voice == "af_bella"


def test_unknown_tag_is_left_as_text():
    turns = parse.parse_markup("[narrator] It began.")
    assert turns[0].text == "[narrator] It began."


def test_leading_pause_becomes_lead_ms():
    turns = parse.parse_markup("[pause:0.5s] Hello.")
    assert turns[0].lead_ms == 500
    assert turns[0].text == "Hello."


def test_unbalanced_markup_is_rejected():
    with pytest.raises(parse.MarkupError):
        parse.parse_markup("[voice:af_heart Hello.")


@pytest.mark.parametrize(
    "text",
    [
        "[pause:soon] Hi.",
        "[rate:9] Hi.",
        "[voice:bad voice] Hi.",
        "[emotion:1] Hi.",
    ],
)
def test_invalid_values_are_rejected(text):
    with pytest.raises(parse.MarkupError):
        parse.parse_markup(text)


def test_turn_list_input():
    turns = parse.parse_input(
        [
            {"text": "Did it land?", "voice": "af_bella", "instructions": "anxious"},
            {"text": "It did.", "voice": "am_michael"},
        ],
        "af_heart",
    )
    assert [t.voice for t in turns] == ["af_bella", "am_michael"]
    assert turns[0].instructions == "anxious"


def test_turn_list_requires_text():
    with pytest.raises(parse.MarkupError):
        parse.parse_input([{"voice": "af_bella"}])


def test_max_input_chars():
    from dataclasses import replace

    from kokoro_tone.limits import DEFAULT_LIMITS

    with pytest.raises(parse.MarkupError):
        parse.parse_input("x" * 20, None, replace(DEFAULT_LIMITS, input_chars=10))


def test_has_markup_only_matches_known_tags():
    assert parse.has_markup("[voice:af_bella] hi")
    assert not parse.has_markup("[narrator] hi")
    assert not parse.has_markup("plain text")


def test_split_long_keeps_prosody_and_gap_on_last_chunk():
    turn = parse.Turn(
        text=("Sentence one. " * 40).strip(),
        voice="af_heart",
        emotion="warm",
        gap_ms=700,
    )
    chunks = parse.split_long([turn], 100)

    assert len(chunks) > 1
    assert all(len(chunk.text) <= 100 for chunk in chunks)
    assert all(chunk.voice == "af_heart" and chunk.emotion == "warm" for chunk in chunks)
    assert chunks[-1].gap_ms == 700
    assert all(chunk.gap_ms == 0 for chunk in chunks[:-1])
