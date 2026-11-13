"""`instructions` and `[emotion:]` mapped onto measurable prosody.

Kokoro's source is fixed, so "emotion" here is rate, pitch, gain and spectral
tilt. Nothing in this module claims to add acting.

These settings are applied after the static per-voice casting gain, so the preset
gain is relative to an already-levelled voice (ADR-0013).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import filters
from .limits import DEFAULT_LIMITS, Limits
from .parse import EMOTION_RESET, Turn

# Defaults, kept as module constants for readability and for the tests that
# pin the preset table. Runtime bounds come from `limits.Limits`.
RATE_MIN = DEFAULT_LIMITS.rate_min
RATE_MAX = DEFAULT_LIMITS.rate_max
PITCH_MIN = DEFAULT_LIMITS.pitch_min
PITCH_MAX = DEFAULT_LIMITS.pitch_max


@dataclass(frozen=True)
class Preset:
    rate: float
    pitch_st: float
    gain_db: float
    filters: tuple[str, ...] = ()


NEUTRAL = Preset(rate=1.0, pitch_st=0.0, gain_db=0.0)

# ffmpeg filter fragments. `bass`/`treble` are shelf filters; the lowpass and
# highpass entries are the "darker"/"hushed" tilt.
PRESETS: dict[str, Preset] = {
    # Warmth is body plus a soft top. The bass shelf stays small on purpose: the
    # tone chain already adds +2 dB at 250 Hz, and a large preset shelf on top of
    # it made this the boomy preset rather than the warm one.
    "warm": Preset(
        0.95,
        -1.2,
        1.0,
        ("bass=g=1.0:f=250:w=0.7", "treble=g=-1.0:f=6000:w=0.7"),
    ),
    # Tilt moves from both ends: a treble shelf and a low-mid cut. The cut is
    # about a third of the effect and lets the shelf stay 2.5 dB smaller, which
    # is what keeps sibilants in check. Calibrated against the measured
    # bm_lewis(4)+bm_fable(1) blend: +0.36 dB/oct and +1.3 dB of HF.
    "bright": Preset(
        1.036,
        1.1,
        1.0,
        ("treble=g=5.0:f=6000:w=0.7", "bass=g=-1.0:f=600:w=1.0"),
    ),
    "sad": Preset(0.88, -2.0, -3.0, ("lowpass=f=6000",)),
    "tired": Preset(0.90, -1.0, -2.0, ("lowpass=f=8000",)),
    "angry": Preset(
        1.15,
        1.0,
        4.0,
        ("treble=g=2:f=6000:w=0.7", "acompressor=ratio=3:attack=10:release=150"),
    ),
    "whisper": Preset(0.85, 0.0, -8.0, ("highpass=f=300", "lowpass=f=8000")),
    # Brighter than `bright` by design. The +2 st pitch costs about 0.2 dB/oct of
    # measured tilt, so the shelf has to clear that as well as the tone chain.
    "excited": Preset(
        1.12,
        2.0,
        2.0,
        ("treble=g=9.0:f=6000:w=0.7", "bass=g=-1.0:f=600:w=1.0"),
    ),
}

# Longest keyword wins, so "not angry, just tired" resolves to `tired`.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "warm": (
        "warm",
        "warmly",
        "soft",
        "softly",
        "gentle",
        "gently",
        "kind",
        "friendly",
        "calm",
        "calmly",
        "soothing",
    ),
    "bright": (
        "bright",
        "brightly",
        "cheerful",
        "cheerfully",
        "upbeat",
        "happy",
        "happily",
        "lively",
        "sunny",
    ),
    "sad": (
        "sad",
        "sadly",
        "sorrow",
        "sorrowful",
        "melancholy",
        "somber",
        "sombre",
        "gloomy",
        "mournful",
    ),
    "tired": (
        "tired",
        "tiredly",
        "weary",
        "wearily",
        "exhausted",
        "sleepy",
        "drowsy",
        "flat",
        "bored",
    ),
    "angry": ("angry", "angrily", "furious", "shout", "shouting", "mad", "rage", "stern", "harsh"),
    "whisper": ("whisper", "whispering", "hushed", "quiet", "quietly", "intimate"),
    "excited": ("excited", "excitedly", "thrilled", "eager", "enthusiastic"),
}

# Precompiled at import: `match_instructions` runs per turn and the keyword set
# is constant. Dict order is preserved, which keeps the longest-match tie-break.
_KEYWORD_PATTERNS: tuple[tuple[str, int, re.Pattern[str]], ...] = tuple(
    (preset, len(word), re.compile(rf"\b{re.escape(word)}\b"))
    for preset, words in KEYWORDS.items()
    for word in words
)


class UnknownEmotion(ValueError):
    """An `[emotion:]` value with no preset behind it."""


@dataclass(frozen=True)
class Prosody:
    rate: float
    pitch_st: float
    gain_db: float
    filters: tuple[str, ...]
    preset: str | None


def match_instructions(text: str | None) -> str | None:
    """Map free-text `instructions` onto a preset name, if any keyword hits."""
    if not text:
        return None
    lowered = text.lower()
    best: str | None = None
    best_length = 0
    for preset, length, pattern in _KEYWORD_PATTERNS:
        if length <= best_length:
            continue
        if pattern.search(lowered):
            best = preset
            best_length = length
    return best


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def resolve(
    turn: Turn,
    request_speed: float = 1.0,
    request_instructions: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> Prosody:
    """Turn-level preset beats request-level instructions beats neutral."""
    # `parse_markup` already maps `[emotion:neutral]` to None; this keeps a
    # hand-built Turn(emotion="neutral") meaning the same thing.
    emotion = None if turn.emotion == EMOTION_RESET else turn.emotion
    if emotion and emotion not in PRESETS:
        known = ", ".join(sorted(PRESETS))
        raise UnknownEmotion(f"unknown emotion {emotion!r}; known: {known}")

    preset_name = (
        emotion or match_instructions(turn.instructions) or match_instructions(request_instructions)
    )
    preset = PRESETS.get(preset_name or "", NEUTRAL)

    speed = request_speed if request_speed and request_speed > 0 else 1.0
    rate = preset.rate * (turn.rate or 1.0) * speed

    chain = preset.filters
    if turn.filter:
        fragment = filters.render_spec(turn.filter)
        if fragment is not None:
            chain = (*chain, fragment)

    return Prosody(
        rate=_clamp(rate, limits.rate_min, limits.rate_max),
        pitch_st=_clamp(preset.pitch_st, limits.pitch_min, limits.pitch_max),
        gain_db=preset.gain_db,
        filters=chain,
        preset=preset_name,
    )
