"""Text and voice shaping for a speech request.

This is the layer between a request body and the script compiler: sanitising
text, resolving a voice name or alias map, rewriting a turn through the
pronunciation rules, and deciding whether a request is passthrough. It has no
pydantic model, no HTTP, no audio and no environment reads, so both the script
compiler and the HTTP route can depend on it without a cycle.

`request.py` owns the OpenAI body and the model catalog; `script.py` owns the
order of operations. This module owns the pure functions they share.
"""

from __future__ import annotations

from . import lexicon, normalise, parse, pronounce
from .errors import RequestError
from .limits import Limits


def clean_instructions(value: str | None, limits: Limits) -> str | None:
    if not value:
        return None
    text = parse.sanitise_text(value)
    if len(text) > limits.instructions_chars:
        raise RequestError(f"instructions exceeds {limits.instructions_chars} characters")
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
        raise RequestError(f"unknown sense tag: {listed}")
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
        default_alias = lexicon_data.default_alias
        return server, _resolve_alias(default_alias, server) if default_alias else None

    if isinstance(voice, str):
        value = voice.strip()
        if not value:
            return server, None
        try:
            parse.validate_voice(value, limits.voice_chars)
        except parse.MarkupError as exc:
            raise RequestError(str(exc)) from exc
        return server, _resolve_alias(value, server)

    if not isinstance(voice, dict):
        raise RequestError("voice must be a string or an object of alias: voice")

    aliases: dict[str, str] = dict(server)
    default: str | None = None
    added = 0

    for name, target in voice.items():
        if not isinstance(name, str):
            raise RequestError("voice map must be an object of alias: voice")
        name = name.strip()

        if name == "id":
            default = _voice_id(target, limits)
            continue

        target = _voice_id(target, limits)
        if not name or not target:
            raise RequestError("voice map entries need an alias and a voice")
        try:
            parse.validate_voice(name, limits.voice_chars)
        except parse.MarkupError as exc:
            raise RequestError(f"invalid voice map: {exc}") from exc
        aliases[name.casefold()] = target
        added += 1

    if added > limits.aliases:
        raise RequestError(f"voice map exceeds {limits.aliases} aliases")
    if not aliases and not default:
        raise RequestError("voice map must define at least one alias")

    fallback = default or lexicon_data.default_alias or next(iter(aliases.values()))
    return aliases, _resolve_alias(fallback, aliases)


def _voice_id(value: object, limits: Limits) -> str:
    """Accept a voice name or the OpenAI `{"id": ...}` object."""
    if isinstance(value, dict):
        value = value.get("id")
    if not isinstance(value, str):
        raise RequestError("voice map values must be a voice name or {'id': ...}")
    value = value.strip()
    try:
        parse.validate_voice(value, limits.voice_chars)
    except parse.MarkupError as exc:
        raise RequestError(str(exc)) from exc
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
