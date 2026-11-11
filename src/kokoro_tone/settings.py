"""Deployment settings, read once at import and reloaded by the tests.

`env.py` is the leaf that reads and validates individual variables; `limits.py`
owns the request bounds. This module composes them into the one `Settings`
object the gateway passes around, plus the loudness constants the health check
and the mastering target share.

Nothing here imports the HTTP layer or the lexicon, so there is no cycle and a
test can reload this module alone to re-read the environment.
"""

from __future__ import annotations

import math
import os
import sys
from dataclasses import dataclass

from . import env, normalise
from .limits import Limits, load_limits

# -16 LUFS is the usual podcast deliverable. The lower bound leaves room for
# ACX-style audiobooks (-20) and quiet narration; the upper bound keeps enough
# peak headroom that the +4 dB `angry` preset is not driven into the -1 dBTP
# limiter, which would flatten the emotion presets.
PODCAST_LUFS = -16.0
LOUDNESS_MIN = -30.0
LOUDNESS_MAX = -12.0


@dataclass(frozen=True)
class Settings:
    upstream_base_url: str
    upstream_api_key: str
    upstream_model: str
    gateway_keys: tuple[str, ...]
    default_voice: str
    loudness_lufs: float
    voice_levelling: bool
    limits: Limits
    normalise: normalise.NormaliseSettings
    lexicon_path: str
    lexicon_overlay_path: str


def _token_list(*values: str) -> tuple[str, ...]:
    """Merge comma-separated token lists, dropping blanks and duplicates."""
    tokens: list[str] = []
    for value in values:
        for token in value.split(","):
            token = token.strip()
            if token and token not in tokens:
                tokens.append(token)
    return tuple(tokens)


def _loudness() -> float:
    raw = os.environ.get("LOUDNESS_LUFS", "")
    try:
        target = float(raw) if raw.strip() else PODCAST_LUFS
    except ValueError:
        target = PODCAST_LUFS
    if not math.isfinite(target):
        target = PODCAST_LUFS
    clamped = max(LOUDNESS_MIN, min(LOUDNESS_MAX, target))
    if clamped != target:
        print(
            f"LOUDNESS_LUFS {target} is outside {LOUDNESS_MIN}..{LOUDNESS_MAX}; using {clamped}",
            file=sys.stderr,
        )
    return clamped


def load_settings() -> Settings:
    return Settings(
        upstream_base_url=os.environ.get("UPSTREAM_BASE_URL", "http://kokoro:8880/v1").rstrip("/"),
        # One secret in .env: an explicit UPSTREAM_API_KEY wins (OpenRouter
        # fallback), otherwise the engine key is reused.
        upstream_api_key=os.environ.get("UPSTREAM_API_KEY")
        or os.environ.get("KOKORO_FASTAPI_API_KEY", ""),
        upstream_model=os.environ.get("UPSTREAM_MODEL", "kokoro"),
        gateway_keys=_token_list(
            os.environ.get("KOKORO_TONE_API_KEYS", ""),
            os.environ.get("KOKORO_TONE_API_KEY", ""),
        ),
        default_voice=os.environ.get("DEFAULT_VOICE", "af_heart"),
        loudness_lufs=_loudness(),
        voice_levelling=env.read_bool("VOICE_LEVELLING", True),
        limits=load_limits(),
        normalise=normalise.load_normalise_settings(),
        lexicon_path=os.environ.get("PRONUNCIATION_LEXICON", "/etc/kokoro-tone/lexicon.json"),
        # Optional user overlay, composed in memory over the shipped defaults.
        # Missing is normal: the defaults alone are a complete lexicon.
        lexicon_overlay_path=os.environ.get(
            "PRONUNCIATION_OVERRIDES", "/etc/kokoro-tone/custom.json"
        ),
    )


SETTINGS = load_settings()
