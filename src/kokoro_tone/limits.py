"""Configurable bounds for the gateway's own features.

Every limit is an environment variable with a default that is safe for a
LAN-only service. Raising one is a deliberate choice: these values are what
stop a single request from becoming unbounded work (thousands of engine calls,
minutes of inserted silence, an arbitrarily large body).
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, replace
from typing import Any, cast

from .env import read_float, read_int


@dataclass(frozen=True)
class Limits:
    # Prosody. Rate is a multiplier; pitch is semitones. The defaults are the
    # range the presets were measured in - wider values start to sound broken.
    rate_min: float = 0.75
    rate_max: float = 1.3
    pitch_min: float = -3.0
    pitch_max: float = 3.0

    # Silence. `pause_ms` caps one `[pause:]`; `total_pause_ms` caps the sum
    # across a request, so a script cannot insert minutes of dead air.
    pause_ms: int = 30_000
    total_pause_ms: int = 60_000

    # Request shape.
    turns: int = 100
    aliases: int = 64
    instructions_chars: int = 2_000
    input_chars: int = 4_096
    voice_chars: int = 200
    body_bytes: int = 1_048_576

    # Rendering.
    segment_chars: int = 1_000
    turn_gap_ms: int = 350

    # OpenAI `speed`, kept separate from the preset rate multiplier.
    speed_min: float = 0.25
    speed_max: float = 4.0

    # Pronunciation mapping and normalisation. `pronunciations` caps one
    # request; `lexicon_rules` caps the deployment file, so a bad file cannot
    # turn every request into a scan of thousands of rules.
    pronunciations: int = 256
    pronunciation_chars: int = 200
    context_words: int = 16
    # How far a `before` cue looks back when a rule sets no explicit window.
    # An `after` cue looks to the end of the sentence instead, because a word
    # after the match is a lexical signal ("bass in the band") while a word
    # before it is usually syntax ("they record"), which is only reliable
    # adjacent.
    before_words: int = 2
    lexicon_rules: int = 2_048
    lexicon_bytes: int = 262_144
    number_words: int = 64
    voice_sets: int = 32


DEFAULT_LIMITS = Limits()


def load_limits() -> Limits:
    limits = replace(
        DEFAULT_LIMITS,
        rate_min=read_float("MIN_RATE", DEFAULT_LIMITS.rate_min),
        rate_max=read_float("MAX_RATE", DEFAULT_LIMITS.rate_max),
        pitch_min=read_float("MIN_PITCH", DEFAULT_LIMITS.pitch_min),
        pitch_max=read_float("MAX_PITCH", DEFAULT_LIMITS.pitch_max),
        pause_ms=read_int("MAX_PAUSE_MS", DEFAULT_LIMITS.pause_ms),
        total_pause_ms=read_int("MAX_TOTAL_PAUSE_MS", DEFAULT_LIMITS.total_pause_ms),
        turns=read_int("MAX_TURNS", DEFAULT_LIMITS.turns),
        aliases=read_int("MAX_ALIASES", DEFAULT_LIMITS.aliases),
        instructions_chars=read_int("MAX_INSTRUCTIONS_CHARS", DEFAULT_LIMITS.instructions_chars),
        input_chars=read_int("MAX_INPUT_CHARS", DEFAULT_LIMITS.input_chars),
        voice_chars=read_int("MAX_VOICE_CHARS", DEFAULT_LIMITS.voice_chars),
        body_bytes=read_int("MAX_BODY_BYTES", DEFAULT_LIMITS.body_bytes),
        segment_chars=read_int("SEGMENT_CHARS", DEFAULT_LIMITS.segment_chars),
        turn_gap_ms=read_int("TURN_GAP_MS", DEFAULT_LIMITS.turn_gap_ms),
        speed_min=read_float("MIN_SPEED", DEFAULT_LIMITS.speed_min),
        speed_max=read_float("MAX_SPEED", DEFAULT_LIMITS.speed_max),
    )
    return _repair(limits)


def _repair(limits: Limits) -> Limits:
    """A min above its max would clamp every request to nonsense."""
    fixes: dict[str, object] = {}
    for low, high in (
        ("rate_min", "rate_max"),
        ("pitch_min", "pitch_max"),
        ("speed_min", "speed_max"),
    ):
        if getattr(limits, low) >= getattr(limits, high):
            print(
                f"{low} >= {high}; using the default {low}/{high}",
                file=sys.stderr,
            )
            fixes[low] = getattr(DEFAULT_LIMITS, low)
            fixes[high] = getattr(DEFAULT_LIMITS, high)
    if limits.total_pause_ms < limits.pause_ms:
        fixes["total_pause_ms"] = limits.pause_ms
    return replace(limits, **cast(Any, fixes)) if fixes else limits
