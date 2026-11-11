import math
import shutil
import struct

import pytest

from kokoro_tone import audio, prosody
from kokoro_tone.parse import Turn

pytestmark = pytest.mark.skipif(
    shutil.which("ffmpeg") is None, reason="ffmpeg is required for audio tests"
)


def tone(seconds: float, frequency: float = 220.0, amplitude: float = 0.2) -> bytes:
    frames = int(audio.SAMPLE_RATE * seconds)
    data = bytearray()
    for index in range(frames):
        value = amplitude * math.sin(2 * math.pi * frequency * index / audio.SAMPLE_RATE)
        data += struct.pack("<h", int(value * 32767))
    return bytes(data)


def two_tone(seconds: float, parts: list[tuple[float, float]]) -> bytes:
    frames = int(audio.SAMPLE_RATE * seconds)
    data = bytearray()
    for index in range(frames):
        value = sum(
            amplitude * math.sin(2 * math.pi * frequency * index / audio.SAMPLE_RATE)
            for frequency, amplitude in parts
        )
        data += struct.pack("<h", int(max(-1.0, min(1.0, value)) * 32767))
    return bytes(data)


def samples(pcm: bytes) -> tuple[int, ...]:
    count = len(pcm) // 2
    return struct.unpack(f"<{count}h", pcm[: count * 2])


def rms(pcm: bytes) -> float:
    data = samples(pcm)
    if not data:
        return 0.0
    return math.sqrt(sum(value * value for value in data) / len(data))


def amplitude_at(pcm: bytes, frequency: float) -> float:
    """DFT bin at an exact frequency - enough to measure a shelf's effect."""
    data = samples(pcm)
    if not data:
        return 0.0
    omega = 2 * math.pi * frequency / audio.SAMPLE_RATE
    real = sum(value * math.cos(omega * index) for index, value in enumerate(data))
    imaginary = sum(value * math.sin(omega * index) for index, value in enumerate(data))
    return 2 * math.hypot(real, imaginary) / len(data)


def zero_cross_frequency(pcm: bytes) -> float:
    data = samples(pcm)
    if len(data) < 2:
        return 0.0
    crossings = sum(
        1 for index in range(1, len(data)) if (data[index - 1] < 0) != (data[index] < 0)
    )
    return crossings * audio.SAMPLE_RATE / (2 * len(data))


def synthesise(pcm: bytes):
    return lambda text, voice: pcm


# --- primitives -------------------------------------------------------------


def test_silence_length_matches_requested_ms():
    assert len(audio.silence(500)) == audio.SAMPLE_RATE * 2 // 2
    assert len(audio.silence(1000)) == audio.SAMPLE_RATE * 2
    assert audio.silence(0) == b""


def test_semitone_ratio():
    assert audio.semitone_ratio(0) == 1.0
    assert audio.semitone_ratio(12) == pytest.approx(2.0)
    assert audio.semitone_ratio(-12) == pytest.approx(0.5)


# --- per-turn prosody -------------------------------------------------------


def test_prosody_changes_duration_in_the_expected_direction():
    source = tone(1.0)
    faster = audio.apply_prosody(source, prosody.Prosody(1.3, 0.0, 0.0, (), "excited"))
    slower = audio.apply_prosody(source, prosody.Prosody(0.8, 0.0, 0.0, (), "sad"))

    assert audio.duration_seconds(faster) < 1.0
    assert audio.duration_seconds(slower) > 1.0


def test_pitch_shift_moves_f0_by_the_requested_semitones():
    source = tone(0.5, frequency=220.0)
    down = audio.apply_prosody(source, prosody.Prosody(1.0, -2.0, 0.0, (), "sad"))
    up = audio.apply_prosody(source, prosody.Prosody(1.0, 2.0, 0.0, (), "excited"))

    assert zero_cross_frequency(down) == pytest.approx(220.0 * 2 ** (-2 / 12), rel=0.03)
    assert zero_cross_frequency(up) == pytest.approx(220.0 * 2 ** (2 / 12), rel=0.03)


def test_preset_gain_is_applied():
    source = tone(0.5, amplitude=0.1)
    quiet = audio.apply_prosody(source, prosody.Prosody(1.0, 0.0, -8.0, (), "whisper"))

    assert rms(quiet) == pytest.approx(rms(source) * 10 ** (-8 / 20), rel=0.05)


# --- every emotion ----------------------------------------------------------

# The table from ADR-0009. Keeping it here means a preset edit has to be
# deliberate: the numbers are pinned, not just the shape.
ADR_PRESETS = {
    "warm": (0.95, -1.0, 1.0),
    "bright": (1.05, 1.0, 1.0),
    "sad": (0.88, -2.0, -3.0),
    "tired": (0.90, -1.0, -2.0),
    "angry": (1.15, 1.0, 4.0),
    "whisper": (0.85, 0.0, -8.0),
    "excited": (1.12, 2.0, 2.0),
}

EQ_EXPECTATIONS = {
    "warm": {"low": "up"},
    "bright": {"high": "up"},
    "sad": {"high": "down"},
    "tired": {"high": "down"},
    "angry": {"high": "up"},
    "whisper": {"low": "down", "high": "down"},
    "excited": {"high": "up"},
}


def test_preset_table_matches_the_adr():
    assert set(prosody.PRESETS) == set(ADR_PRESETS)
    for emotion, (rate, pitch, gain) in ADR_PRESETS.items():
        preset = prosody.PRESETS[emotion]
        assert (preset.rate, preset.pitch_st, preset.gain_db) == (rate, pitch, gain)


@pytest.mark.parametrize("emotion", sorted(ADR_PRESETS))
def test_each_emotion_applies_its_rate_and_pitch(emotion):
    source = tone(1.0, frequency=220.0)
    preset = prosody.PRESETS[emotion]
    out = audio.apply_prosody(source, prosody.resolve(Turn(text="x", emotion=emotion)))

    assert audio.duration_seconds(out) == pytest.approx(1.0 / preset.rate, rel=0.04)
    if abs(preset.pitch_st) > 0.1:
        expected = 220.0 * 2 ** (preset.pitch_st / 12)
        assert zero_cross_frequency(out) == pytest.approx(expected, rel=0.05)


@pytest.mark.parametrize("emotion", sorted(ADR_PRESETS))
def test_each_emotion_applies_its_gain(emotion):
    # Compared against the same preset with gain removed, so the pitch shifter's
    # own level change cancels and only the `volume` stage is measured.
    source = tone(1.0, frequency=1000.0, amplitude=0.1)
    preset = prosody.PRESETS[emotion]
    resolved = prosody.resolve(Turn(text="x", emotion=emotion))

    out = audio.apply_prosody(source, resolved)
    reference = audio.apply_prosody(
        source,
        prosody.Prosody(resolved.rate, resolved.pitch_st, 0.0, resolved.filters, emotion),
    )

    expected = rms(reference) * 10 ** (preset.gain_db / 20)
    assert rms(out) == pytest.approx(expected, rel=0.1)


@pytest.mark.parametrize("emotion", sorted(EQ_EXPECTATIONS))
def test_each_emotion_eq_moves_the_documented_band(emotion):
    source = two_tone(0.5, [(120.0, 0.02), (10000.0, 0.02)])
    preset = prosody.PRESETS[emotion]
    # Neutral rate/pitch/gain: this measures the preset's filter set alone.
    settings = prosody.Prosody(1.0, 0.0, 0.0, preset.filters, emotion)
    out = audio.apply_prosody(source, settings)

    low = amplitude_at(out, 120.0) / amplitude_at(source, 120.0)
    high = amplitude_at(out, 10000.0) / amplitude_at(source, 10000.0)

    expectation = EQ_EXPECTATIONS[emotion]
    if expectation.get("low") == "up":
        assert low > 1.05
    elif expectation.get("low") == "down":
        assert low < 0.9
    if expectation.get("high") == "up":
        assert high > 1.05
    elif expectation.get("high") == "down":
        assert high < 0.9


# --- warmth master ----------------------------------------------------------


def test_warmth_boosts_lows_and_cuts_highs():
    # Low level on purpose: keeps the compressor and saturator idle so the
    # shelves are measured on their own.
    source = two_tone(0.5, [(120.0, 0.02), (10000.0, 0.02)])
    warmed = audio.apply_warmth(source)

    assert amplitude_at(warmed, 120.0) > amplitude_at(source, 120.0) * 1.12
    assert amplitude_at(warmed, 10000.0) < amplitude_at(source, 10000.0) * 0.92


def test_warmth_compresses_dynamic_range():
    # Two levels, both above the 0.125 threshold: 4:1 in, ~2.5:1 out.
    source = tone(1.0, amplitude=0.8) + tone(1.0, amplitude=0.2)
    warmed = audio.apply_warmth(source)

    def window(pcm: bytes, start_s: float, length_s: float) -> bytes:
        start = int(audio.SAMPLE_RATE * start_s) * 2
        end = start + int(audio.SAMPLE_RATE * length_s) * 2
        return pcm[start:end]

    before = rms(window(source, 0.3, 0.4)) / rms(window(source, 1.3, 0.4))
    after = rms(window(warmed, 0.3, 0.4)) / rms(window(warmed, 1.3, 0.4))

    assert before > 3.5
    assert after < before * 0.8


def test_master_hits_loudness_target_and_peak_ceiling():
    mastered = audio.master(tone(2.0, amplitude=0.05), -16.0)
    lufs, true_peak = audio.measure_loudness(mastered)

    assert lufs == pytest.approx(-16.0, abs=1.0)
    assert true_peak <= -1.0 + 0.2


def test_master_limiter_holds_the_ceiling_when_gain_would_clip():
    # A target above the source's loudness forces positive gain into the limiter.
    mastered = audio.master(tone(1.0, amplitude=0.9), 0.0)
    _lufs, true_peak = audio.measure_loudness(mastered)

    assert true_peak <= -1.0 + 0.2


def test_master_does_not_normalise_segments():
    # 20 dB apart going in; one linear gain must keep them far apart coming out.
    source = tone(0.5, amplitude=0.03) + tone(0.5, amplitude=0.3)
    mastered = audio.master(source, -16.0)
    middle = len(mastered) // 2

    quiet = rms(mastered[:middle])
    loud = rms(mastered[middle:])

    assert loud > quiet * 3.0


# --- encode -----------------------------------------------------------------


def test_encode_formats_are_recognisable():
    pcm = tone(0.25)
    mp3, mp3_type = audio.encode(pcm, "mp3")
    wav, wav_type = audio.encode(pcm, "wav")

    assert mp3_type == "audio/mpeg"
    assert wav_type == "audio/wav"
    assert wav[:4] == b"RIFF"
    assert len(mp3) > 0


# --- render -----------------------------------------------------------------


def test_render_sums_turns_and_gaps():
    turns = [
        Turn(text="One.", voice="af_heart"),
        Turn(text="Two.", voice="am_michael", gap_ms=700),
        Turn(text="Three.", voice="bf_emma"),
    ]
    rendered = audio.render(
        turns, synthesise(tone(0.5)), speed=1.0, target_lufs=-16.0, turn_gap_ms=350
    )

    # 3 x 0.5 s of speech + 0.7 s explicit pause + 0.35 s default gap
    assert audio.duration_seconds(rendered) == pytest.approx(1.5 + 0.7 + 0.35, abs=0.1)


def test_render_masters_to_target_loudness():
    rendered = audio.render(
        [Turn(text="One.")],
        synthesise(tone(1.0, amplitude=0.1)),
        target_lufs=-16.0,
        turn_gap_ms=0,
    )
    lufs, true_peak = audio.measure_loudness(rendered)

    assert lufs == pytest.approx(-16.0, abs=1.0)
    assert true_peak <= -1.0 + 0.2


def test_render_applies_turn_emotion():
    sad = audio.render([Turn(text="A.", emotion="sad")], synthesise(tone(1.0)), turn_gap_ms=0)
    excited = audio.render(
        [Turn(text="B.", emotion="excited")], synthesise(tone(1.0)), turn_gap_ms=0
    )

    assert audio.duration_seconds(sad) > audio.duration_seconds(excited)


def test_render_keeps_a_whispered_turn_quieter_than_a_normal_one():
    turns = [Turn(text="One.", emotion="whisper"), Turn(text="Two.")]
    rendered = audio.render(turns, synthesise(tone(1.0)), target_lufs=-16.0, turn_gap_ms=0)

    window = int(audio.SAMPLE_RATE * 0.5) * 2
    whispered = rms(rendered[:window])
    normal = rms(rendered[-window:])

    assert whispered < normal * 0.75


def test_emotion_range_survives_the_hottest_allowed_target():
    # -12 LUFS is the loudest target the service accepts; the -8 dB whisper
    # must still be clearly quieter than a neutral turn.
    turns = [Turn(text="One.", emotion="whisper"), Turn(text="Two.")]
    rendered = audio.render(turns, synthesise(tone(1.0)), target_lufs=-12.0, turn_gap_ms=0)

    window = int(audio.SAMPLE_RATE * 0.5) * 2
    whispered = rms(rendered[:window])
    normal = rms(rendered[-window:])

    assert whispered < normal * 0.5
