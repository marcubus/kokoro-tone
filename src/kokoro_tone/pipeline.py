"""The order of operations for one speech request.

`prepare` validates and shapes the body; `render` turns prepared turns into
mastered bytes. No FastAPI here: the route owns status codes, headers and SSE.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import audio, levelling, parse, pronounce, request, script
from .context import Context
from .errors import RequestError
from .parse import Turn
from .request import SpeechRequest


@dataclass(frozen=True)
class Prepared:
    turns: list[Turn]
    voice: str
    payload: object
    instructions: str | None
    flags: pronounce.Flags
    found: list[pronounce.Ambiguity]
    ambiguity: str
    stream_format: str
    passthrough: bool

    @property
    def chars(self) -> int:
        return sum(len(turn.text) for turn in self.turns)


@dataclass(frozen=True)
class Rendered:
    data: bytes
    media_type: str
    upstream_ms: float


def volume_multiplier(voice: str | None, ctx: Context) -> float | None:
    """The static casting gain for a voice, or None when it is unity/disabled."""
    if not ctx.settings.voice_levelling:
        return None
    value = levelling.multiplier(voice or ctx.settings.default_voice)
    return value if abs(value - 1.0) > 1e-4 else None


def _validate(body: SpeechRequest, ctx: Context) -> str:
    limits = ctx.settings.limits
    request.check_model(body.model)

    if body.speed is not None and not (limits.speed_min <= body.speed <= limits.speed_max):
        raise RequestError(f"speed must be between {limits.speed_min} and {limits.speed_max}")
    if (body.response_format or "mp3").lower() not in audio.FORMATS:
        known = ", ".join(sorted(audio.FORMATS))
        raise RequestError(f"unsupported response_format; use {known}")
    stream_format = (body.stream_format or "audio").lower()
    if stream_format not in ("audio", "sse"):
        raise RequestError("stream_format must be 'audio' or 'sse'")
    return stream_format


def prepare(body: SpeechRequest, ctx: Context) -> Prepared:
    """Validate the body, then let the script layer rewrite the text into turns."""
    stream_format = _validate(body, ctx)
    compiled = script.compile_script(
        script.ScriptInput(
            input=body.input,
            voice=body.voice,
            instructions=body.instructions,
            pronunciations=body.pronunciations,
            normalise=body.normalise,
            ambiguity=body.ambiguity,
        ),
        lexicon_data=ctx.lexicon.current(),
        settings=ctx.settings,
    )

    return Prepared(
        turns=list(compiled.turns),
        voice=compiled.voice,
        payload=compiled.payload,
        instructions=compiled.instructions,
        flags=compiled.flags,
        found=list(compiled.found),
        ambiguity=compiled.ambiguity,
        stream_format=stream_format,
        passthrough=compiled.passthrough,
    )


def render(prepared: Prepared, body: SpeechRequest, ctx: Context) -> Rendered:
    """Synthesise every turn, master the file and encode it once."""
    limits = ctx.settings.limits
    telemetry = ctx.telemetry
    started = time.perf_counter()
    upstream_ms = 0.0

    def render_turn(text: str, voice: str | None) -> bytes:
        nonlocal upstream_ms
        at = time.perf_counter()
        try:
            return ctx.upstream.synthesise(
                text,
                voice,
                volume_multiplier=volume_multiplier(voice or prepared.voice, ctx),
            )
        finally:
            upstream_ms += (time.perf_counter() - at) * 1000.0

    turns = parse.split_long(prepared.turns, limits.segment_chars)
    pcm = audio.render(
        turns,
        render_turn,
        speed=body.speed or 1.0,
        instructions=prepared.instructions,
        target_lufs=ctx.settings.loudness_lufs,
        limits=limits,
        telemetry=telemetry,
    )
    audio_seconds = audio.duration_seconds(pcm)
    data, media_type = audio.encode(pcm, body.response_format, telemetry=telemetry)
    render_seconds = time.perf_counter() - started
    telemetry.speech_duration.record(render_seconds)
    telemetry.speech_audio_duration.record(audio_seconds)
    return Rendered(data=data, media_type=media_type, upstream_ms=upstream_ms)
