"""Request validation and shaping, before anything reaches the engine.

The OpenAI body is not the shape the renderer wants: a voice can be a string or
an alias map, `input` can be a string with markup or a list of turns, and
`pronunciations` can be a map or an ordered rule list. This module reduces all
of that to turns, voices and rules; the routes only orchestrate.
"""

from __future__ import annotations

from fastapi import HTTPException
from pydantic import BaseModel

from . import lexicon, normalise, parse, pronounce
from .limits import Limits

MODELS = ("kokoro-v1_0",)
ACCEPTED_MODELS = frozenset({"kokoro-v1_0", "kokoro", "tts-1", "tts-1-hd"})
REJECTED_MODELS = frozenset({"gpt-4o-mini-tts"})


class SpeechRequest(BaseModel):
    model: str = "kokoro-v1_0"
    input: object
    # A voice name, or a map of alias -> voice for this request. An `id` entry
    # (string or {"id": ...}) sets the default for untagged text.
    voice: str | dict[str, object] | None = None
    instructions: str | None = None
    response_format: str = "mp3"
    speed: float = 1.0
    # OpenAI's field. `audio` (default) is a binary body; `sse` wraps the same
    # bytes in speech.audio.delta events.
    stream_format: str | None = None
    # Kokoro/OpenRouter's field, kept as an alias for raw audio streaming.
    stream: bool = False
    # Ordered word -> spoken-form rules. A map is the shorthand; a list adds
    # context, case and whole-word control. An object containing `*` is a
    # per-alias map, with `*` as the global part.
    pronunciations: object | None = None
    # The built-in layers: true/false, or {"lexicon", "acronyms", "numbers"}.
    normalise: object | None = None
    # What to do with a heteronym no bare rule settled. "guess" (default) picks
    # the first candidate and reports it in X-Kokoro-Tone-Guessed; "ignore"
    # leaves the engine's own default and says nothing.
    ambiguity: str | None = None


def check_model(model: str | None) -> None:
    name = (model or MODELS[0]).strip()
    if name in REJECTED_MODELS:
        raise HTTPException(
            400,
            "this gateway serves Kokoro only; GPT TTS models are not available",
        )
    if name not in ACCEPTED_MODELS:
        known = ", ".join(sorted(ACCEPTED_MODELS))
        raise HTTPException(400, f"unknown model; use one of {known}")


def clean_instructions(value: str | None, limits: Limits) -> str | None:
    if not value:
        return None
    text = parse.sanitise_text(value)
    if len(text) > limits.instructions_chars:
        raise HTTPException(400, f"instructions exceeds {limits.instructions_chars} characters")
    return text


def sanitise_input(payload: object) -> object:
    if isinstance(payload, str):
        return parse.sanitise_text(payload)
    if isinstance(payload, list):
        cleaned: list[object] = []
        for item in payload:
            if isinstance(item, dict) and isinstance(item.get("text"), str):
                cleaned.append({**item, "text": parse.sanitise_text(item["text"])})
            else:
                cleaned.append(item)
        return cleaned
    return payload


def voice_token(voice: object) -> str | None:
    """The alias or voice name as the caller wrote it, before resolution.

    For a map without an `id`, the first alias is the one that supplies the
    default voice, so it is also the token that selects that voice's set.
    """
    if isinstance(voice, str):
        return voice.strip() or None
    if isinstance(voice, dict):
        value = voice.get("id")
        if isinstance(value, str):
            return value.strip() or None
        if isinstance(value, dict) and isinstance(value.get("id"), str):
            return value["id"].strip() or None
        for key in voice:
            if isinstance(key, str) and key.strip() and key.strip() != "id":
                return key.strip()
    return None


def rewrite(
    text: str,
    token: str | None,
    voice: str | None,
    request_global: tuple[lexicon.Rule, ...],
    request_alias: dict[str, tuple[lexicon.Rule, ...]],
    lexicon_data: lexicon.LexiconData,
    flags: pronounce.Flags,
    normalise_settings: normalise.NormaliseSettings,
    limits: Limits,
    ambiguities: list[pronounce.Ambiguity] | None = None,
    guess: bool = False,
    markers: list[str] | tuple[str, ...] = (),
) -> str:
    effective = pronounce.effective_set(
        token, voice, request_global, request_alias, lexicon_data, normalise_settings
    )
    rewriter = pronounce.build_rewriter(effective, flags, normalise_settings, limits)
    rewritten = rewriter.apply(text, ambiguities=ambiguities, guess=guess)
    # A sense tag desugars to a marker a rule must resolve. Anything left means
    # the word or sense is not in the lexicon, so say so instead of reading it.
    unknown = parse.unresolved_markers(rewritten, markers)
    if unknown:
        listed = ", ".join(dict.fromkeys(f"[sense:{item.replace('|', '=')}]" for item in unknown))
        raise HTTPException(400, f"unknown sense tag: {listed}")
    return rewritten


def voice_spec(
    voice: object, limits: Limits, lexicon_data: lexicon.LexiconData
) -> tuple[dict[str, str], str | None]:
    """Split `voice` into (alias map, default voice).

    A string is a voice name (existing behaviour). A map defines aliases for
    this request only; an `id` entry supplies the default voice. Server aliases
    from the pronunciation file sit underneath the request map, so a client can
    override any of them and a client that sends no map inherits them.
    """
    server = dict(lexicon_data.aliases)
    if voice is None:
        default = lexicon_data.default_alias
        return server, _resolve_alias(default, server) if default else None

    if isinstance(voice, str):
        value = voice.strip()
        if not value:
            return server, None
        try:
            parse.validate_voice(value, limits.voice_chars)
        except parse.MarkupError as exc:
            raise HTTPException(400, str(exc)) from exc
        return server, _resolve_alias(value, server)

    if not isinstance(voice, dict):
        raise HTTPException(400, "voice must be a string or an object of alias: voice")

    aliases: dict[str, str] = dict(server)
    default: str | None = None
    added = 0

    for name, target in voice.items():
        if not isinstance(name, str):
            raise HTTPException(400, "voice map must be an object of alias: voice")
        name = name.strip()

        if name == "id":
            default = _voice_id(target, limits)
            continue

        target = _voice_id(target, limits)
        if not name or not target:
            raise HTTPException(400, "voice map entries need an alias and a voice")
        try:
            parse.validate_voice(name, limits.voice_chars)
        except parse.MarkupError as exc:
            raise HTTPException(400, f"invalid voice map: {exc}") from exc
        aliases[name.casefold()] = target
        added += 1

    if added > limits.aliases:
        raise HTTPException(400, f"voice map exceeds {limits.aliases} aliases")
    if not aliases and not default:
        raise HTTPException(400, "voice map must define at least one alias")

    fallback = default or lexicon_data.default_alias or next(iter(aliases.values()))
    return aliases, _resolve_alias(fallback, aliases)


def _voice_id(value: object, limits: Limits) -> str:
    """Accept a voice name or the OpenAI `{"id": ...}` object."""
    if isinstance(value, dict):
        value = value.get("id")
    if not isinstance(value, str):
        raise HTTPException(400, "voice map values must be a voice name or {'id': ...}")
    value = value.strip()
    try:
        parse.validate_voice(value, limits.voice_chars)
    except parse.MarkupError as exc:
        raise HTTPException(400, str(exc)) from exc
    return value


def _resolve_alias(name: str, aliases: dict[str, str]) -> str:
    """Follow alias -> alias chains, stopping on a cycle or a real voice."""
    seen: set[str] = set()
    current = name
    while True:
        key = current.strip().casefold()
        if key not in aliases or key in seen:
            return current
        seen.add(key)
        current = aliases[key]


def apply_aliases(turns: list[parse.Turn], aliases: dict[str, str]) -> None:
    if not aliases:
        return
    for turn in turns:
        if turn.voice:
            turn.voice = _resolve_alias(turn.voice, aliases)


def is_passthrough(payload: object, instructions: str | None) -> bool:
    """Untagged, uninstructed strings are forwarded whole, streaming intact."""
    return isinstance(payload, str) and not parse.has_markup(payload) and not instructions
