import pytest

from kokoro_tone import main


def test_loudness_defaults_to_the_podcast_target(monkeypatch):
    monkeypatch.delenv("LOUDNESS_LUFS", raising=False)

    assert main.load_settings().loudness_lufs == pytest.approx(main.PODCAST_LUFS)
    assert main.PODCAST_LUFS == -16.0


def test_loudness_accepts_acx_and_mono_podcast_targets(monkeypatch):
    for value in ("-20", "-19", "-14"):
        monkeypatch.setenv("LOUDNESS_LUFS", value)
        assert main.load_settings().loudness_lufs == pytest.approx(float(value))


def test_loudness_is_clamped_to_the_emotion_safe_range(monkeypatch):
    monkeypatch.setenv("LOUDNESS_LUFS", "-6")
    assert main.load_settings().loudness_lufs == pytest.approx(main.LOUDNESS_MAX)

    monkeypatch.setenv("LOUDNESS_LUFS", "-45")
    assert main.load_settings().loudness_lufs == pytest.approx(main.LOUDNESS_MIN)


def test_loudness_falls_back_on_nonsense(monkeypatch):
    monkeypatch.setenv("LOUDNESS_LUFS", "loud")
    assert main.load_settings().loudness_lufs == pytest.approx(main.PODCAST_LUFS)


def test_emotion_range_fits_inside_the_loudness_bounds():
    # The loudest preset (+4 dB) must stay clear of the -1 dBTP ceiling at the
    # hottest allowed target, and the quietest (-8 dB) must stay audible.
    assert main.LOUDNESS_MAX + 4.0 < -1.0
    assert main.LOUDNESS_MIN - 8.0 > -60.0


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
