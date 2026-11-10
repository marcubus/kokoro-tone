"""ffmpeg audio chain: per-turn prosody, silence, warmth master, encode.

Kokoro is asked for raw PCM, so the gateway decodes nothing and encodes once.
That is what keeps "double encoding" out of the signal path.

Filter expressions are built only from constants and formatted floats. Before
any expression reaches ffmpeg it is checked against a strict character class,
so a future edit that interpolates client text fails loudly instead of running.
"""

from __future__ import annotations

import json
import re
import subprocess

from . import prosody as prosody_mod
from .limits import DEFAULT_LIMITS, Limits
from .parse import Turn

SAMPLE_RATE = 24000
BYTES_PER_SAMPLE = 2
CHANNELS = 1

# Whole-file warmth master. Applied once, after concatenation.
WARMTH_CHAIN = (
    "bass=g=2.0:f=250:w=0.7",
    "treble=g=-1.5:f=6000:w=0.7",
    "acompressor=ratio=2.5:attack=15:release=200:threshold=0.125:makeup=1",
    "asoftclip=type=tanh:threshold=0.7",
)
LIMITER = "alimiter=limit=0.891:level=0"  # -1 dBFS ceiling

# Everything the chain legitimately contains: filter names, numeric values,
# units and the separators ffmpeg uses. Nothing else may appear.
_FILTER_SAFE = re.compile(r"^[A-Za-z0-9_=:.,+\- ]*$")

FORMATS: dict[str, tuple[tuple[str, ...], str]] = {
    "mp3": (("-c:a", "libmp3lame", "-b:a", "192k", "-f", "mp3"), "audio/mpeg"),
    "wav": (("-c:a", "pcm_s16le", "-f", "wav"), "audio/wav"),
    "flac": (("-c:a", "flac", "-f", "flac"), "audio/flac"),
    "opus": (("-c:a", "libopus", "-b:a", "64k", "-f", "ogg"), "audio/ogg"),
    "aac": (("-c:a", "aac", "-b:a", "128k", "-f", "adts"), "audio/aac"),
    "pcm": (("-f", "s16le"), "audio/pcm"),
}


class AudioError(RuntimeError):
    """ffmpeg failed or produced something unusable."""


def _filters(values: list[str]) -> str:
    """Join a filter chain, refusing anything outside the known grammar."""
    text = ",".join(values)
    if not _FILTER_SAFE.match(text):
        raise AudioError("refusing to run an unexpected filter expression")
    return text


def _pcm_input() -> list[str]:
    return [
        "-f",
        "s16le",
        "-ar",
        str(SAMPLE_RATE),
        "-ac",
        str(CHANNELS),
        "-i",
        "pipe:0",
    ]


def _pcm_output() -> list[str]:
    return ["-f", "s16le", "-ar", str(SAMPLE_RATE), "-ac", str(CHANNELS), "pipe:1"]


def _run(args: list[str], data: bytes | None = None) -> bytes:
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-loglevel", "error", "-nostdin", "-y", *args],
            input=data,
            capture_output=True,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioError(f"ffmpeg could not run: {exc}") from exc
    if proc.returncode != 0:
        detail = proc.stderr.decode("utf-8", "replace").strip()[-800:]
        raise AudioError(detail or f"ffmpeg exited {proc.returncode}")
    return proc.stdout


_rubberband: bool | None = None


def has_rubberband() -> bool:
    """Detected once: Debian's ffmpeg has it, a stripped build may not."""
    global _rubberband
    if _rubberband is None:
        try:
            proc = subprocess.run(
                ["ffmpeg", "-hide_banner", "-filters"],
                capture_output=True,
                text=True,
                timeout=30,
                check=False,
            )
            _rubberband = re.search(r"^\s*\S+\s+rubberband\b", proc.stdout, re.M) is not None
        except (OSError, subprocess.SubprocessError):
            _rubberband = False
    return _rubberband


def semitone_ratio(semitones: float) -> float:
    return 2.0 ** (semitones / 12.0)


def silence(ms: int) -> bytes:
    samples = int(round(SAMPLE_RATE * ms / 1000.0))
    return b"\x00\x00" * max(samples, 0)


def duration_seconds(pcm: bytes) -> float:
    frame = BYTES_PER_SAMPLE * CHANNELS
    return len(pcm) / float(SAMPLE_RATE * frame)


def apply_prosody(pcm: bytes, settings: prosody_mod.Prosody) -> bytes:
    """Pitch, tempo and preset EQ for one turn."""
    filters: list[str] = []
    rate = settings.rate

    if abs(settings.pitch_st) > 1e-3:
        ratio = semitone_ratio(settings.pitch_st)
        if has_rubberband():
            filters.append(f"rubberband=pitch={ratio:.6f}:formant=preserved")
        else:
            # Shifts formants too; only reached on an ffmpeg without librubberband.
            filters.append(f"asetrate={int(round(SAMPLE_RATE * ratio))}")
            filters.append(f"aresample={SAMPLE_RATE}")
            rate /= ratio

    if abs(rate - 1.0) > 1e-3:
        filters.append(f"atempo={rate:.6f}")
    if abs(settings.gain_db) > 1e-3:
        filters.append(f"volume={settings.gain_db:.2f}dB")
    filters.extend(settings.filters)

    if not filters:
        return pcm
    filters.append(f"aresample={SAMPLE_RATE}")
    return _run([*_pcm_input(), "-af", _filters(filters), *_pcm_output()], pcm)


def measure_loudness(pcm: bytes) -> tuple[float, float]:
    """Integrated LUFS and true peak of the input, via loudnorm's analysis."""
    args = [
        *_pcm_input(),
        "-af",
        "loudnorm=I=-16:TP=-1:LRA=11:print_format=json",
        "-f",
        "null",
        "-",
    ]
    try:
        proc = subprocess.run(
            ["ffmpeg", "-hide_banner", "-nostats", "-nostdin", *args],
            input=pcm,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            timeout=300,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AudioError(f"ffmpeg could not run: {exc}") from exc
    stderr = proc.stderr.decode("utf-8", "replace")
    blocks = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr, re.S)
    if proc.returncode != 0 or not blocks:
        raise AudioError(stderr.strip()[-800:] or "loudnorm produced no measurement")
    try:
        data = json.loads(blocks[-1])
        return float(data["input_i"]), float(data["input_tp"])
    except (ValueError, KeyError) as exc:
        raise AudioError(f"unreadable loudnorm output: {exc}") from exc


def apply_warmth(pcm: bytes) -> bytes:
    """Shelves, compression and saturation — no gain, no limiter."""
    return _run(
        [*_pcm_input(), "-af", _filters(list(WARMTH_CHAIN)), *_pcm_output()],
        pcm,
    )


def master(pcm: bytes, target_lufs: float = -16.0) -> bytes:
    """Warmth chain, then one linear gain to target, then a -1 dBTP ceiling.

    Loudness is measured after the chain (the compressor and shelves move it)
    and corrected once, across the whole file. Per-segment normalisation would
    boost a whispered line back to normal and erase it.
    """
    warmed = apply_warmth(pcm)

    measured, _peak = measure_loudness(warmed)
    gain_db = 0.0
    if measured != float("-inf"):
        gain_db = max(-30.0, min(30.0, target_lufs - measured))

    filters: list[str] = []
    if abs(gain_db) > 0.05:
        filters.append(f"volume={gain_db:.2f}dB")
    filters.append(LIMITER)
    filters.append(f"aresample={SAMPLE_RATE}")

    return _run([*_pcm_input(), "-af", _filters(filters), *_pcm_output()], warmed)


def encode(pcm: bytes, response_format: str) -> tuple[bytes, str]:
    fmt = (response_format or "mp3").lower()
    if fmt not in FORMATS:
        known = ", ".join(sorted(FORMATS))
        raise ValueError(f"unsupported response_format {response_format!r}; use {known}")
    args, media_type = FORMATS[fmt]
    return _run([*_pcm_input(), *args, "pipe:1"], pcm), media_type


def render(
    turns: list[Turn],
    synthesize,
    *,
    speed: float = 1.0,
    instructions: str | None = None,
    target_lufs: float = -16.0,
    turn_gap_ms: int | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> bytes:
    """Render turns to mastered PCM. `synthesize(text, voice) -> PCM`."""
    if not turns:
        raise AudioError("nothing to render")

    parts: list[bytes] = []
    if turns[0].lead_ms:
        parts.append(silence(turns[0].lead_ms))

    for index, turn in enumerate(turns):
        settings = prosody_mod.resolve(turn, speed, instructions, limits)
        pcm = synthesize(turn.text, turn.voice)
        if not pcm:
            raise AudioError("upstream returned empty audio")
        parts.append(apply_prosody(pcm, settings))

        last = index == len(turns) - 1
        default_gap = limits.turn_gap_ms if turn_gap_ms is None else turn_gap_ms
        gap = turn.gap_ms if turn.gap_ms > 0 else (0 if last else default_gap)
        if gap > 0:
            parts.append(silence(gap))

    return master(b"".join(parts), target_lufs)
