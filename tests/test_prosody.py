import pytest

from kokoro_tone import prosody
from kokoro_tone.parse import Turn


@pytest.mark.parametrize(
    ("instructions", "expected"),
    [
        ("speak warmly", "warm"),
        ("she is sad", "sad"),
        ("ANGRY, shouting", "angry"),
        ("a tired sigh", "tired"),
        ("whisper it", "whisper"),
        ("bright and cheerful", "bright"),
        ("nothing recognisable here", None),
        (None, None),
    ],
)
def test_instruction_keywords(instructions, expected):
    assert prosody.match_instructions(instructions) == expected


def test_longest_keyword_wins():
    assert prosody.match_instructions("not angry, just tired") == "tired"


def test_turn_emotion_overrides_instructions():
    turn = Turn(text="Hi.", emotion="sad")
    result = prosody.resolve(turn, 1.0, "excited")
    assert result.preset == "sad"
    assert result.pitch_st < 0
    assert result.rate < 1.0


def test_neutral_emotion_falls_back_to_request_instructions():
    # `[emotion:neutral]` clears the turn override, so the request-level
    # `instructions` apply again rather than nothing.
    result = prosody.resolve(Turn(text="Hi.", emotion="neutral"), 1.0, "warm")
    assert result.preset == "warm"
    assert result.rate < 1.0


def test_neutral_emotion_alone_is_neutral():
    result = prosody.resolve(Turn(text="Hi.", emotion="neutral"))
    assert result.preset is None
    assert (result.rate, result.pitch_st, result.gain_db) == (1.0, 0.0, 0.0)


def test_unknown_emotion_raises():
    with pytest.raises(prosody.UnknownEmotion):
        prosody.resolve(Turn(text="Hi.", emotion="smug"))


def test_rate_and_pitch_are_clamped():
    turn = Turn(text="Hi.", emotion="excited", rate=4.0)
    result = prosody.resolve(turn, 4.0)
    assert result.rate == prosody.RATE_MAX
    assert result.pitch_st == prosody.PRESETS["excited"].pitch_st
    assert prosody.PITCH_MIN <= result.pitch_st <= prosody.PITCH_MAX


def test_neutral_when_nothing_matches():
    result = prosody.resolve(Turn(text="Hi."), 1.0, None)
    assert result.preset is None
    assert (result.rate, result.pitch_st, result.gain_db) == (1.0, 0.0, 0.0)
    assert result.filters == ()
