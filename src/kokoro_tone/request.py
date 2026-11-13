"""The OpenAI speech body and the model catalog.

This module is the transport-facing shape: the pydantic model a route receives
and the model names it accepts or rejects. Text and voice shaping live in
`text.py`; the order of operations lives in `script.py` and `pipeline.py`.
"""

from __future__ import annotations

from pydantic import BaseModel

from .errors import RequestError

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
    # leaves the engine's own default and says nothing; "error" stops with a 400.
    ambiguity: str | None = None


def check_model(model: str | None) -> None:
    name = (model or MODELS[0]).strip()
    if name in REJECTED_MODELS:
        raise RequestError("this gateway serves Kokoro only; GPT TTS models are not available")
    if name not in ACCEPTED_MODELS:
        known = ", ".join(sorted(ACCEPTED_MODELS))
        raise RequestError(f"unknown model; use one of {known}")
