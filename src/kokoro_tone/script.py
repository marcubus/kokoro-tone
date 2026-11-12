"""The script layer: interpret a script into turns of spoken text.

This is the stable, engine-independent half of the gateway. It takes the same
script a client sends - plain prose, sticky `[voice:]`/`[emotion:]`/`[rate:]`/
`[pause:]`/`[sense:]`/`[filter:]` markup, or an array of turns - and resolves
pronunciations, heteronyms, acronyms and numbers into the text the engine
should say. It knows nothing about HTTP, audio rendering or Kokoro. A `[filter:]`
tag is validated against the declarative block registry, but no ffmpeg process
runs here.

`pipeline.prepare` calls this; the `kokoro_tone.script_cli` entry point calls it
without a server, which is what makes the script layer usable on its own.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Protocol

from . import lexicon, parse, pronounce, request
from .errors import AmbiguityError, RequestError
from .lexicon import LexiconData
from .limits import DEFAULT_LIMITS, Limits
from .normalise import NormaliseSettings
from .parse import Turn


class ScriptSettings(Protocol):
    """The three settings the script layer actually reads.

    `settings.Settings` satisfies this structurally, so the gateway passes its
    deployment settings unchanged. A library caller can pass `ScriptConfig`
    instead and avoid bootstrapping the environment.
    """

    @property
    def limits(self) -> Limits: ...

    @property
    def normalise(self) -> NormaliseSettings: ...

    @property
    def default_voice(self) -> str: ...


@dataclass(frozen=True)
class ScriptConfig:
    """Defaults for a standalone script compile: no environment, no server."""

    limits: Limits = DEFAULT_LIMITS
    normalise: NormaliseSettings = field(default_factory=NormaliseSettings)
    default_voice: str = "af_heart"


@dataclass(frozen=True)
class ScriptInput:
    """The text-facing subset of a speech request."""

    input: object
    voice: object | None = None
    instructions: str | None = None
    pronunciations: object | None = None
    normalise: object | None = None
    ambiguity: str | None = None


@dataclass(frozen=True)
class Compiled:
    turns: tuple[Turn, ...]
    voice: str
    payload: object
    instructions: str | None
    flags: pronounce.Flags
    found: tuple[pronounce.Ambiguity, ...]
    ambiguity: str
    passthrough: bool

    @property
    def chars(self) -> int:
        return sum(len(turn.text) for turn in self.turns)

    @property
    def text(self) -> str:
        """The spoken text, one turn per line. The simplest thing to consume."""
        return "\n".join(turn.text for turn in self.turns)

    def as_dict(self) -> dict[str, object]:
        """A JSON-ready view: the spoken text, each turn, and every guess."""
        return {
            "text": self.text,
            "turns": [
                {
                    "text": turn.text,
                    "voice": turn.voice,
                    "emotion": turn.emotion,
                    "rate": turn.rate,
                    "filter": turn.filter.as_text() if turn.filter else None,
                    "gap_ms": turn.gap_ms,
                    "lead_ms": turn.lead_ms,
                }
                for turn in self.turns
            ],
            "voice": self.voice,
            "instructions": self.instructions,
            "ambiguity": self.ambiguity,
            "ambiguities": [asdict(item) for item in self.found],
            "passthrough": self.passthrough,
            "chars": self.chars,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.as_dict(), indent=indent)


def _ambiguity(value: str | None) -> str:
    mode = (value or "guess").strip().lower()
    if mode not in ("guess", "ignore", "error"):
        raise RequestError("ambiguity must be 'guess', 'ignore' or 'error'")
    return mode


def _raise_ambiguity(found: list[pronounce.Ambiguity]) -> None:
    """Stop rather than ship a guess, with the tags the caller can paste in."""
    if not found:
        return
    listed = ", ".join(f"{item.word} ({', '.join(item.options)})" for item in found)
    noun = "word needs" if len(found) == 1 else "words need"
    message = (
        f"{len(found)} {noun} a sense tag: {listed}. "
        "Write the tag in the text, or add a pronunciation rule for it."
    )
    payload = [
        {
            "word": item.word,
            "offset": item.offset,
            "options": list(item.options),
            "says": list(item.says),
            "chosen": item.chosen,
        }
        for item in found
    ]
    raise AmbiguityError(message, payload)


def compile_script(
    data: ScriptInput,
    *,
    lexicon_data: LexiconData | None = None,
    settings: ScriptSettings | None = None,
) -> Compiled:
    """Validate the script and rewrite every turn into its spoken form.

    Both `lexicon_data` and `settings` are optional, so a library caller can do
    `compile_script(ScriptInput(input="..."))` with no environment. The gateway
    passes its deployment lexicon and `Settings`.
    """
    config = settings or ScriptConfig()
    data_lexicon = lexicon_data or lexicon.EMPTY_LEXICON
    limits = config.limits

    instructions = request.clean_instructions(data.instructions, limits)
    aliases, voice = request.voice_spec(data.voice, limits, data_lexicon)
    default_voice = voice or config.default_voice
    payload = request.sanitise_input(data.input)
    ambiguity = _ambiguity(data.ambiguity)

    try:
        flags = pronounce.parse_flags(data.normalise, config.normalise)
        request_global, request_alias = lexicon.parse_request_sets(data.pronunciations, limits)
    except parse.MarkupError as exc:
        raise RequestError(str(exc)) from exc

    token = request.voice_token(data.voice)
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
            data_lexicon,
            flags,
            config.normalise,
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
                data_lexicon,
                flags,
                config.normalise,
                limits,
                collect,
                guessing,
                turn.markers,
            )
        total = sum(len(turn.text) for turn in turns)
        if total > limits.input_chars:
            raise RequestError(f"input exceeds {limits.input_chars} characters")

    if ambiguity == "error":
        _raise_ambiguity(found)

    return Compiled(
        turns=tuple(turns),
        voice=default_voice,
        payload=payload,
        instructions=instructions,
        flags=flags,
        found=tuple(found),
        ambiguity=ambiguity,
        passthrough=request.is_passthrough(payload, instructions),
    )


def compile_text(
    text: str,
    *,
    lexicon_data: LexiconData | None = None,
    settings: ScriptSettings | None = None,
    voice: object | None = None,
    normalise: object | None = None,
) -> Compiled:
    """Compile one string with no request shape.

    The common one-liner with a deployment lexicon:

        data = load_lexicon("lexicon/lexicon.json", DEFAULT_LIMITS)
        compile_text("The bass was loud.", lexicon_data=data, normalise=True).text
    """
    return compile_script(
        ScriptInput(input=text, voice=voice, normalise=normalise),
        lexicon_data=lexicon_data,
        settings=settings,
    )
