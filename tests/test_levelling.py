import pytest

from kokoro_tone import levelling


def test_the_table_covers_every_measured_voice():
    assert len(levelling.VOICE_GAIN_DB) == 72
    assert levelling.VOICE_GAIN_DB["af_nova"] == pytest.approx(6.7)
    assert levelling.VOICE_GAIN_DB["am_adam"] == pytest.approx(-2.5)
    assert levelling.VOICE_GAIN_DB["am_v0gurney"] == pytest.approx(10.4)
    assert levelling.VOICE_GAIN_DB["bf_isabella"] == pytest.approx(-2.3)


def test_components_parse_a_single_voice():
    assert levelling.components("af_heart") == [("af_heart", 1.0)]


def test_components_parse_weighted_and_subtractive_blends():
    assert levelling.components("af_bella(2)+af_sky(1)") == [
        ("af_bella", 2.0),
        ("af_sky", 1.0),
    ]
    assert levelling.components("af_bella(3) - af_sky(1)") == [
        ("af_bella", 3.0),
        ("af_sky", -1.0),
    ]


def test_components_reject_something_the_engine_would_reject():
    assert levelling.components("af_bella+") == []
    assert levelling.components("af_bella(2") == []
    assert levelling.components("") == []


def test_gain_is_the_measured_value_for_a_single_voice():
    assert levelling.gain_db("af_nova") == pytest.approx(6.7)
    assert levelling.gain_db("am_adam") == pytest.approx(-2.5)


def test_gain_interpolates_a_weighted_blend():
    # (2 * 0.5 + 1 * 3.9) / 3
    assert levelling.gain_db("af_bella(2)+af_sky(1)") == pytest.approx(1.6333, abs=1e-4)


def test_gain_handles_a_subtractive_blend():
    # (3 * 0.5 - 1 * 3.9) / (3 + 1)
    assert levelling.gain_db("af_bella(3)-af_sky(1)") == pytest.approx(-0.6)


def test_unknown_voices_stay_at_unity():
    assert levelling.gain_db("mystery") == 0.0
    assert levelling.multiplier("mystery") == 1.0
    assert levelling.gain_db(None) == 0.0


def test_openai_aliases_resolve_to_the_measured_voice():
    assert levelling.gain_db("alloy") == pytest.approx(-2.5)
    assert levelling.gain_db("shimmer") == pytest.approx(3.9)
    assert levelling.gain_db("nova") == pytest.approx(-2.3)
    assert levelling.gain_db("ALLOY") == pytest.approx(-2.5)


def test_the_multiplier_is_the_linear_gain():
    assert levelling.multiplier("af_nova") == pytest.approx(10 ** (6.7 / 20), rel=1e-3)
    assert levelling.multiplier("if_sara") == pytest.approx(10 ** (-5.6 / 20), rel=1e-3)
