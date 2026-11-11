"""Turn and inline-markup parsing for the OpenAI speech contract.

`input` is either a plain string carrying markup or the OpenRouter-style list
of turns. Both reduce to a list of `Turn`, which is all the renderer needs.

Everything a client sends is treated as data: the tag grammar is a closed
allowlist, values are validated against anchored patterns, and control
characters are stripped before the text reaches the engine.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .limits import DEFAULT_LIMITS, Limits

TAG_NAMES = ("voice", "emotion", "rate", "pause", "sense")

# `[emotion:neutral]` clears a sticky emotion instead of selecting a preset. It is
# the only accepted value with no preset behind it, so it reads as "stop
# overriding" rather than as a tone.
EMOTION_RESET = "neutral"

# `[tag:value]` - values may not contain brackets, which keeps the tokenizer
# from swallowing a neighbouring tag when one is malformed.
_TAG_RE = re.compile(r"\[([A-Za-z_]+):([^\[\]]*)\]")
_OPENER_RE = re.compile(r"\[([A-Za-z_]+):")
_PAUSE_RE = re.compile(r"^(\d+(?:\.\d+)?)\s*(ms|s)?$", re.IGNORECASE)
_VOICE_RE = re.compile(r"^[A-Za-z0-9_.+()\-]+$")
_EMOTION_RE = re.compile(r"^[A-Za-z][A-Za-z_-]*$")
# `[sense:word=sense]` names which reading of a heteronym to use. The word is a
# normal English token; the sense is the hyphenated key the lexicon publishes.
_SENSE_RE = re.compile(r"^([A-Za-z][A-Za-z'\-]*)=([a-z0-9]+(?:-[a-z0-9]+)*)$")
# NUL and the C0 controls except tab and newline. DEL and C1 are dropped too.
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f]")


class MarkupError(ValueError):
    """Raised for anything the caller should see as a 400."""


@dataclass
class Turn:
    text: str
    voice: str | None = None
    emotion: str | None = None
    rate: float | None = None
    instructions: str | None = None
    gap_ms: int = 0  # silence *after* this turn
    lead_ms: int = 0  # silence *before* this turn (only set on the first)
    # Markers a `[sense:]` tag desugared to. Kept so an unresolved one can be
    # reported without treating a literal `|` in prose as a marker.
    markers: list[str] = field(default_factory=list)


def sanitise_text(value: str) -> str:
    """Drop control characters, keeping tabs and newlines."""
    return _CONTROL_RE.sub("", value)


def _short(value: object) -> str:
    """Echo at most 40 characters of client input back in an error."""
    text = str(value)
    return text if len(text) <= 40 else f"{text[:37]}..."


def has_markup(text: str) -> bool:
    """True when the string carries any tag the gateway would act on."""
    for match in _TAG_RE.finditer(text):
        if match.group(1).lower() in TAG_NAMES:
            return True
    return False


def _check_balanced(text: str) -> None:
    for match in _OPENER_RE.finditer(text):
        if match.group(1).lower() not in TAG_NAMES:
            continue
        rest = text[match.end() :]
        close = rest.find("]")
        nxt = rest.find("[")
        if close == -1 or (nxt != -1 and nxt < close):
            snippet = text[match.start() : match.start() + 24]
            raise MarkupError(f"unbalanced markup near {_short(snippet)}")


def _parse_pause(value: str, limits: Limits = DEFAULT_LIMITS) -> int:
    match = _PAUSE_RE.match(value)
    if not match:
        raise MarkupError(f"invalid pause value {_short(value)}; use e.g. [pause:1.2s]")
    amount = float(match.group(1))
    unit = (match.group(2) or "s").lower()
    ms = amount if unit == "ms" else amount * 1000
    if ms > limits.pause_ms:
        raise MarkupError(f"pause {_short(value)} exceeds {limits.pause_ms} ms")
    return int(round(ms))


def _parse_rate(value: str, limits: Limits = DEFAULT_LIMITS) -> float:
    try:
        rate = float(value)
    except ValueError as exc:
        raise MarkupError(f"invalid rate {_short(value)}") from exc
    if not (limits.rate_min <= rate <= limits.rate_max):
        raise MarkupError(f"rate {rate} outside {limits.rate_min}-{limits.rate_max}")
    return rate


def validate_voice(value: str, max_chars: int = 200) -> str:
    if not value or len(value) > max_chars or not _VOICE_RE.match(value):
        raise MarkupError(f"invalid voice {_short(value)}")
    return value


def _parse_sense(value: str, limits: Limits = DEFAULT_LIMITS) -> str:
    """`word=sense` -> the internal marker a pronunciation rule matches."""
    match = _SENSE_RE.match(value)
    if not match:
        raise MarkupError(f"invalid sense tag {_short(value)}; use [sense:word=sense]")
    word, sense = match.groups()
    if len(word) > limits.pronunciation_chars or len(sense) > limits.pronunciation_chars:
        raise MarkupError(f"sense tag exceeds {limits.pronunciation_chars} characters")
    return f"{word}|{sense}"


def unresolved_markers(text: str, markers: list[str] | tuple[str, ...] = ()) -> list[str]:
    """Sense markers from tags that survived rewriting, so no rule resolved them."""
    return [marker for marker in dict.fromkeys(markers) if marker in text]


def _desugar_senses(text: str, limits: Limits) -> tuple[str, list[str]]:
    """Replace `[sense:word=sense]` tags with the markers a rule matches."""
    markers: list[str] = []

    def replace(match: re.Match[str]) -> str:
        if match.group(1).lower() != "sense":
            return match.group(0)
        marker = _parse_sense(match.group(2), limits)
        markers.append(marker)
        return marker

    return _TAG_RE.sub(replace, text), markers


def _parse_emotion(value: str) -> str | None:
    """Normalise an emotion tag. `neutral` clears a sticky emotion.

    Clearing is not a preset: it removes the turn-level override so the request's
    `instructions` (or nothing) applies again. It has to be expressed, because
    tags are sticky and a script otherwise cannot return to the default.
    """
    if not value or not _EMOTION_RE.match(value):
        raise MarkupError(f"invalid emotion {_short(value)}")
    name = value.lower()
    return None if name == EMOTION_RESET else name


def parse_markup(
    text: str,
    default_voice: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> list[Turn]:
    """Split a marked-up string into turns.

    Tags are sticky: `[voice:af_heart]` applies until the next `[voice:]`.
    Unknown `[tag:value]` pairs are left in the text untouched, so a script
    that happens to contain brackets is not silently mangled.
    """
    text = sanitise_text(text)
    _check_balanced(text)

    turns: list[Turn] = []
    buffer: list[str] = []
    pending_markers: list[str] = []
    pos = 0
    leading_pause = 0
    total_pause = 0
    state: dict[str, object] = {
        "voice": default_voice,
        "emotion": None,
        "rate": None,
    }

    def flush() -> None:
        raw = "".join(buffer)
        buffer.clear()
        cleaned = raw.strip()
        if cleaned:
            turns.append(
                Turn(text=cleaned, markers=pending_markers.copy(), **state)  # type: ignore[arg-type]
            )
        pending_markers.clear()

    for match in _TAG_RE.finditer(text):
        name = match.group(1).lower()
        if name not in TAG_NAMES:
            continue
        if name == "sense":
            # A sense tag substitutes in place, so it must not break the turn.
            buffer.append(text[pos : match.start()])
            marker = _parse_sense(match.group(2), limits)
            buffer.append(marker)
            pending_markers.append(marker)
            pos = match.end()
            continue
        buffer.append(text[pos : match.start()])
        pos = match.end()
        flush()
        value = match.group(2).strip()
        if name == "pause":
            gap = _parse_pause(value, limits)
            total_pause += gap
            if total_pause > limits.total_pause_ms:
                raise MarkupError(f"total pause exceeds {limits.total_pause_ms} ms")
            if turns:
                turns[-1].gap_ms += gap
            else:
                leading_pause += gap
        elif name == "voice":
            state["voice"] = validate_voice(value, limits.voice_chars)
        elif name == "emotion":
            state["emotion"] = _parse_emotion(value)
        else:  # rate
            state["rate"] = _parse_rate(value, limits)

    buffer.append(text[pos:])
    flush()

    if not turns:
        raise MarkupError("input produced no speech")
    if leading_pause:
        turns[0].lead_ms = leading_pause
    return turns


def _optional_float(value: object, limits: Limits = DEFAULT_LIMITS) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise MarkupError(f"invalid rate {_short(value)}")
    return _parse_rate(str(value), limits)


def parse_input(
    payload: object,
    default_voice: str | None = None,
    limits: Limits = DEFAULT_LIMITS,
) -> list[Turn]:
    """Normalise either accepted `input` shape into turns."""
    if isinstance(payload, str):
        text = sanitise_text(payload)
        if len(text) > limits.input_chars:
            raise MarkupError(f"input exceeds {limits.input_chars} characters")
        return parse_markup(text, default_voice, limits)

    if isinstance(payload, list):
        if len(payload) > limits.turns:
            raise MarkupError(f"input exceeds {limits.turns} turns")
        turns: list[Turn] = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("text"), str):
                raise MarkupError("each input turn needs a 'text' string")
            text, markers = _desugar_senses(sanitise_text(item["text"]), limits)
            text = text.strip()
            if not text:
                continue
            emotion = item.get("emotion")
            if emotion is not None and not isinstance(emotion, str):
                raise MarkupError("turn 'emotion' must be a string")
            instructions = item.get("instructions")
            if instructions is not None and not isinstance(instructions, str):
                raise MarkupError("turn 'instructions' must be a string")
            if instructions:
                instructions = sanitise_text(instructions)
                if len(instructions) > limits.instructions_chars:
                    raise MarkupError(
                        f"turn 'instructions' exceeds {limits.instructions_chars} characters"
                    )
            voice = item.get("voice") or default_voice
            if voice is not None:
                if not isinstance(voice, str):
                    raise MarkupError("turn 'voice' must be a string")
                voice = validate_voice(voice.strip(), limits.voice_chars)
            turns.append(
                Turn(
                    text=text,
                    voice=voice,
                    emotion=_parse_emotion(emotion) if emotion else None,
                    rate=_optional_float(item.get("rate"), limits),
                    instructions=instructions,
                    markers=markers,
                )
            )
        if not turns:
            raise MarkupError("input produced no speech")
        total = sum(len(turn.text) for turn in turns)
        if total > limits.input_chars:
            raise MarkupError(f"input exceeds {limits.input_chars} characters")
        return turns

    raise MarkupError("input must be a string or a list of turns")


def split_long(turns: list[Turn], limit: int) -> list[Turn]:
    """Split over-long turns at sentence boundaries, keeping their prosody."""
    if limit <= 0:
        return turns
    out: list[Turn] = []
    for turn in turns:
        if len(turn.text) <= limit:
            out.append(turn)
            continue
        chunks = _split_text(turn.text, limit)
        for index, chunk in enumerate(chunks):
            out.append(
                Turn(
                    text=chunk,
                    voice=turn.voice,
                    emotion=turn.emotion,
                    rate=turn.rate,
                    instructions=turn.instructions,
                    gap_ms=turn.gap_ms if index == len(chunks) - 1 else 0,
                    lead_ms=turn.lead_ms if index == 0 else 0,
                    markers=turn.markers,
                )
            )
    return out


def _split_text(text: str, limit: int) -> list[str]:
    sentences = re.split(r"(?<=[.!?\u2026])\s+", text)
    chunks: list[str] = []
    current = ""
    for sentence in sentences:
        while len(sentence) > limit:
            if current:
                chunks.append(current.strip())
                current = ""
            cut = sentence.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            chunks.append(sentence[:cut].strip())
            sentence = sentence[cut:].strip()
        if not sentence:
            continue
        candidate = f"{current} {sentence}".strip()
        if len(candidate) > limit:
            chunks.append(current.strip())
            current = sentence
        else:
            current = candidate
    if current.strip():
        chunks.append(current.strip())
    return [chunk for chunk in chunks if chunk]
