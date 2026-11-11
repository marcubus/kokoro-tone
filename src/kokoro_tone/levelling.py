"""Static per-voice casting gain, so voices start from a common level.

Kokoro does no loudness normalisation: raw output spans 9.2 dB across the 28
English v1.0 voices and 16 dB across all 72 measured voices. The whole-file
master is one linear gain, so it cannot equalise speakers inside a file. This
module supplies the constant, per-voice correction measured in
`docs/voice-loudness.md`: a gain to a common -24 LUFS pre-master target.

The gain is not per-segment `loudnorm`. It is fixed for a voice across the whole
file, so a whispered line stays quiet; only the casting bias is removed. It is
passed to the engine as `volume_multiplier`, which applies it in the float domain
and keeps the passthrough path (unmastered, streaming) levelled too.

Ordering is deliberate (ADR-0013): levelling happens before the per-turn emotion
preset, so the preset's relative gain - `whisper` at -8 dB, `angry` at +4 dB -
survives instead of being normalised away.
"""

from __future__ import annotations

import re

# Gain in dB to a common -24 LUFS pre-master target. Measured with ITU-R BS.1770
# integrated loudness over a ~40 s paragraph at speed 1.0, raw engine output.
# Appendix A of docs/voice-loudness.md; the 28 English v1.0 voices are a subset.
VOICE_GAIN_DB: dict[str, float] = {
    "af_alloy": 2.8,
    "af_amelia_inno": -0.5,
    "af_aoede": -2.3,
    "af_bella": 0.5,
    "af_goodall_inno": 1.0,
    "af_heart": 1.6,
    "af_jadzia": 5.4,
    "af_jessica": 1.3,
    "af_kore": -1.9,
    "af_nicole": 1.0,
    "af_nova": 6.7,
    "af_river": 1.2,
    "af_sarah": 0.1,
    "af_sky": 3.9,
    "af_v0": 2.3,
    "af_v0bella": 2.5,
    "af_v0irulan": 5.1,
    "af_v0nicole": 2.2,
    "af_v0sarah": 1.6,
    "af_v0sky": 7.9,
    "am_adam": -2.5,
    "am_echo": 2.6,
    "am_eric": 1.4,
    "am_fenrir": -1.6,
    "am_liam": 1.1,
    "am_michael": 3.5,
    "am_onyx": 3.9,
    "am_price_inno": 0.6,
    "am_puck": -1.1,
    "am_santa": 1.9,
    "am_v0adam": -1.3,
    "am_v0gurney": 10.4,
    "am_v0michael": 6.3,
    "bf_alice": 0.2,
    "bf_emma": -0.1,
    "bf_isabella": -2.3,
    "bf_lily": 1.0,
    "bf_v0emma": 1.3,
    "bf_v0isabella": -0.1,
    "bm_atten_inno": 0.3,
    "bm_daniel": 0.7,
    "bm_fable": 3.6,
    "bm_george": 1.2,
    "bm_lewis": 2.8,
    "bm_v0george": 5.2,
    "bm_v0lewis": 4.9,
    "ef_dora": 2.8,
    "em_alex": 2.1,
    "em_santa": 1.8,
    "ff_siwis": -0.2,
    "hf_alpha": -3.9,
    "hf_beta": -2.3,
    "hm_omega": -3.0,
    "hm_psi": -1.1,
    "if_sara": -5.6,
    "im_nicola": -0.3,
    "jf_alpha": -1.0,
    "jf_gongitsune": -0.1,
    "jf_nezumi": -1.0,
    "jf_tebukuro": -0.2,
    "jm_kumo": -0.4,
    "pf_dora": 2.9,
    "pm_alex": 2.4,
    "pm_santa": 2.0,
    "zf_xiaobei": -0.3,
    "zf_xiaoni": -1.8,
    "zf_xiaoxiao": -3.4,
    "zf_xiaoyi": -0.8,
    "zm_yunjian": 0.6,
    "zm_yunxi": 2.1,
    "zm_yunxia": -0.1,
    "zm_yunyang": 3.3,
}

# OpenAI voice names are resolved by the engine, not here, so levelling has to
# resolve them too.
OPENAI_ALIASES: dict[str, str] = {
    "alloy": "am_adam",
    "ash": "af_nicole",
    "ballad": "bm_fable",
    "coral": "bf_emma",
    "echo": "af_bella",
    "fable": "af_sarah",
    "marin": "af_heart",
    "nova": "bf_isabella",
    "onyx": "bm_george",
    "sage": "am_michael",
    "shimmer": "af_sky",
    "cedar": "am_fenrir",
    "verse": "am_puck",
}

# One blend term: optional sign, voice name, optional `(weight)`. Spaces are
# stripped first, matching the engine's blend grammar.
_TERM_RE = re.compile(r"([+-]?)([A-Za-z0-9_.]+)(?:\((\d+(?:\.\d+)?)\))?")


def _canonical(name: str) -> str:
    key = name.casefold()
    return OPENAI_ALIASES.get(key, key)


def components(voice: str | None) -> list[tuple[str, float]]:
    """Split a voice or blend into `(name, weight)` terms.

    Returns an empty list for anything the engine's grammar would not accept;
    the engine still owns validation, so an odd string is simply left at unity.
    """
    text = re.sub(r"\s+", "", voice or "")
    if not text:
        return []

    terms: list[tuple[str, float]] = []
    position = 0
    while position < len(text):
        match = _TERM_RE.match(text, position)
        if match is None or match.start() != position:
            return []
        sign, name, weight = match.groups()
        if position > 0 and not sign:
            return []
        value = float(weight) if weight is not None else 1.0
        terms.append((name, -value if sign == "-" else value))
        position = match.end()
    return terms


def gain_db(voice: str | None) -> float:
    """The static gain for a voice or weighted blend, in dB.

    A blend is a weighted sum of style tensors, so its level is estimated as the
    same weighted sum of the member gains, normalised by the absolute weights.
    Unknown voices contribute unity, which keeps an unrecognised voice usable
    instead of failing the request.
    """
    terms = components(voice)
    total = sum(abs(weight) for _, weight in terms)
    if not terms or total <= 0.0:
        return 0.0
    weighted = sum(weight * VOICE_GAIN_DB.get(_canonical(name), 0.0) for name, weight in terms)
    return weighted / total


def multiplier(voice: str | None) -> float:
    """The engine's linear `volume_multiplier` for a voice or blend."""
    value = 10.0 ** (gain_db(voice) / 20.0)
    # The engine accepts 0..10; the measured table never comes close.
    return round(max(0.0, min(10.0, value)), 4)
