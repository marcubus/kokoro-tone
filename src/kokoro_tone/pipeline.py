"""The order of operations for one speech request.

`prepare` validates and shapes the body; `render` turns prepared turns into
mastered bytes. No FastAPI here: the route owns status codes, headers and SSE.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from . import audio, levelling, lexicon, parse, pronounce, request
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


def _validate(body: SpeechRequest, ctx: Context) -> tuple[str, str]:
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
    ambiguity = (body.ambiguity or "guess").strip().lower()
    if ambiguity not in ("guess", "ignore"):
        raise RequestError("ambiguity must be 'guess' or 'ignore'")
    return stream_format, ambiguity


def prepare(body: SpeechRequest, ctx: Context) -> Prepared:
    """Validate the body, resolve voices and rewrite the text into turns."""
    limits = ctx.settings.limits
    stream_format, ambiguity = _validate(body, ctx)

    instructions = request.clean_instructions(body.instructions, limits)
    data = ctx.lexicon.current()
    aliases, voice = request.voice_spec(body.voice, limits, data)
    default_voice = voice or ctx.settings.default_voice
    payload = request.sanitise_input(body.input)

    try:
        flags = pronounce.parse_flags(body.normalise, ctx.settings.normalise)
        request_global, request_alias = lexicon.parse_request_sets(body.pronunciations, limits)
    except parse.MarkupError as exc:
        raise RequestError(str(exc)) from exc

    token = request.voice_token(body.voice)
    found: list[pronounce.Ambiguity] = []
    collect = found if ambiguity != "ignore" else None
    guessing = ambiguity == "guess"
    plain = isinstance(payload, str) and not parse.has_markup(payload)
    if isinstance(payload, str) and plain:
        payload = request.rewrite(
            payload,
            token,
            default_voice,
            request_global,
            request_alias,
            data,
            flags,
            ctx.settings.normalise,
            limits,
            collect,
            guessing,
        )
        if len(payload) > limits.input_chars:
            raise RequestError(f"input exceeds {limits.input_chars} characters")

    try:
        turns = parse.parse_input(payload, default_voice, limits)
    except parse.MarkupError as exc:
        raise RequestError(str(exc)) from exc

    tokens = [turn.voice for turn in turns]
    request.apply_aliases(turns, aliases)

    if not plain:
        for turn, turn_token in zip(turns, tokens, strict=True):
            turn.text = request.rewrite(
                turn.text,
                turn_token,
                turn.voice,
                request_global,
                request_alias,
                data,
                flags,
                ctx.settings.normalise,
                limits,
                collect,
                guessing,
                turn.markers,
            )
        total = sum(len(turn.text) for turn in turns)
        if total > limits.input_chars:
            raise RequestError(f"input exceeds {limits.input_chars} characters")

    return Prepared(
        turns=turns,
        voice=default_voice,
        payload=payload,
        instructions=instructions,
        flags=flags,
        found=found,
        ambiguity=ambiguity,
        stream_format=stream_format,
        passthrough=request.is_passthrough(payload, instructions),
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
