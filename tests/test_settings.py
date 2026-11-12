import pytest

from kokoro_tone import settings


def test_loudness_defaults_to_the_podcast_target(monkeypatch):
    monkeypatch.delenv("LOUDNESS_LUFS", raising=False)

    assert settings.load_settings().loudness_lufs == pytest.approx(settings.PODCAST_LUFS)
    assert settings.PODCAST_LUFS == -16.0


def test_loudness_accepts_acx_and_mono_podcast_targets(monkeypatch):
    for value in ("-20", "-19", "-14"):
        monkeypatch.setenv("LOUDNESS_LUFS", value)
        assert settings.load_settings().loudness_lufs == pytest.approx(float(value))


def test_loudness_is_clamped_to_the_emotion_safe_range(monkeypatch):
    monkeypatch.setenv("LOUDNESS_LUFS", "-6")
    assert settings.load_settings().loudness_lufs == pytest.approx(settings.LOUDNESS_MAX)

    monkeypatch.setenv("LOUDNESS_LUFS", "-45")
    assert settings.load_settings().loudness_lufs == pytest.approx(settings.LOUDNESS_MIN)


def test_loudness_falls_back_on_nonsense(monkeypatch):
    monkeypatch.setenv("LOUDNESS_LUFS", "loud")
    assert settings.load_settings().loudness_lufs == pytest.approx(settings.PODCAST_LUFS)


def test_emotion_range_fits_inside_the_loudness_bounds():
    # The loudest preset (+4 dB) must stay clear of the -1 dBTP ceiling at the
    # hottest allowed target, and the quietest (-8 dB) must stay audible.
    assert settings.LOUDNESS_MAX + 4.0 < -1.0
    assert settings.LOUDNESS_MIN - 8.0 > -60.0


def test_voice_levelling_is_on_by_default_and_can_be_disabled(monkeypatch):
    monkeypatch.delenv("VOICE_LEVELLING", raising=False)
    assert settings.load_settings().voice_levelling is True

    monkeypatch.setenv("VOICE_LEVELLING", "false")
    assert settings.load_settings().voice_levelling is False

    monkeypatch.setenv("VOICE_LEVELLING", "sometimes")
    assert settings.load_settings().voice_levelling is True


def test_limits_defaults_are_the_documented_bounds():
    from kokoro_tone.limits import load_limits

    limits = load_limits()
    assert (limits.rate_min, limits.rate_max) == (0.75, 1.3)
    assert (limits.pitch_min, limits.pitch_max) == (-3.0, 3.0)
    assert limits.pause_ms == 30_000
    assert limits.total_pause_ms == 60_000
    assert limits.turns == 100
    assert limits.aliases == 64
    assert limits.instructions_chars == 2_000
    assert limits.body_bytes == 1_048_576


def test_limits_can_be_extended_from_the_environment(monkeypatch):
    from kokoro_tone.limits import load_limits

    monkeypatch.setenv("MAX_RATE", "1.6")
    monkeypatch.setenv("MAX_PAUSE_MS", "45000")
    monkeypatch.setenv("MAX_TURNS", "250")
    monkeypatch.setenv("MAX_BODY_BYTES", "2097152")

    limits = load_limits()
    assert limits.rate_max == pytest.approx(1.6)
    assert limits.pause_ms == 45_000
    assert limits.turns == 250
    assert limits.body_bytes == 2_097_152


def test_limits_fall_back_when_a_min_exceeds_its_max(monkeypatch):
    from kokoro_tone.limits import load_limits

    monkeypatch.setenv("MIN_RATE", "2.0")
    monkeypatch.setenv("MAX_RATE", "1.0")

    limits = load_limits()
    assert (limits.rate_min, limits.rate_max) == (0.75, 1.3)


def test_limits_fall_back_on_nonsense(monkeypatch):
    from kokoro_tone.limits import load_limits

    monkeypatch.setenv("MAX_TURNS", "lots")
    monkeypatch.setenv("MAX_PAUSE_MS", "-5")

    limits = load_limits()
    assert limits.turns == 100
    assert limits.pause_ms == 30_000


def test_the_pronunciation_limits_have_documented_defaults():
    from kokoro_tone.limits import load_limits

    limits = load_limits()
    assert limits.pronunciations == 256
    assert limits.pronunciation_chars == 200
    assert limits.context_words == 16
    assert limits.before_words == 2
    assert limits.lexicon_rules == 2_048
    assert limits.lexicon_bytes == 262_144
    assert limits.number_words == 64
    assert limits.voice_sets == 32


def test_the_env_readers_fall_back_rather_than_raising(monkeypatch):
    from kokoro_tone import env

    monkeypatch.setenv("MAX_TURNS", "lots")
    monkeypatch.setenv("NORMALISE_DEFAULT", "sometimes")

    assert env.read_int("MAX_TURNS", 100) == 100
    assert env.read_bool("NORMALISE_DEFAULT", False) is False


@pytest.mark.parametrize("raw", ["inf", "-inf", "nan", "3.9", "0.5"])
def test_read_int_rejects_non_whole_values_without_raising(monkeypatch, raw):
    from kokoro_tone import env

    monkeypatch.setenv("MAX_TURNS", raw)

    assert env.read_int("MAX_TURNS", 100) == 100


def test_read_int_keeps_full_precision_and_integral_decimals(monkeypatch):
    from kokoro_tone import env

    monkeypatch.setenv("MAX_BODY_BYTES", "9007199254740993")
    assert env.read_int("MAX_BODY_BYTES", 1) == 9007199254740993

    monkeypatch.setenv("MAX_BODY_BYTES", "4096.0")
    assert env.read_int("MAX_BODY_BYTES", 1) == 4096
