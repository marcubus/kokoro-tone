# Changelog

All notable changes to `kokoro-tone` are recorded here.

Versions use CalVer in the form `YYYY.MM.MICRO`. `MICRO` starts at `0` each
month and increments per release; tags are prefixed with `v`.

## [2026.11.0] - 2026-11-10

### Added

- OpenAI-compatible `POST /v1/audio/speech` in front of Kokoro-FastAPI, with a
  pinned engine version in `engine.version`.
- Prosody presets driven by `instructions` and `[emotion:…]`: warm, bright,
  sad, tired, angry, whisper and excited.
- Whole-file warmth master and a single linear gain to the configured loudness
  target, with a −1 dBTP limiter.
- Multi-turn `input`, sticky markup, voice blends and per-request alias maps
  with an `id` default.
- OpenAI SSE (`speech.audio.delta` / `speech.audio.done`) and the OpenAI error
  envelope.
- Configurable request bounds for rate, pitch, pause, turns, aliases,
  instructions, input and body size.
- Input sanitisation: control characters are stripped, voices and aliases are
  validated, the model name is never forwarded, and ffmpeg filter expressions
  are checked against a strict grammar.
- Bearer auth with a comma-separated token list and constant-time comparison.
