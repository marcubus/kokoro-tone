# Changelog

All notable changes to `kokoro-tone` are recorded here.

Versions use CalVer in the form `YYYY.MM.MICRO`. `MICRO` starts at `0` each
month and increments per release; tags are prefixed with `v`.

## [2026.11.1] - 2026-11-11

### Added

- `pronunciations` on `POST /v1/audio/speech`: ordered literal rules with
  optional context, so a word can be mapped to the form the engine should say
  and words spelt the same can be told apart.
- `normalise`: opt-in digit and acronym layers. Australian English cardinals,
  decimals, percent, ranges and ordinals, with years, versions, model names,
  dates and IPs left alone; acronyms from a table or spelled letter by letter.
- A deployment pronunciation file (`PRONUNCIATION_LEXICON`) with a `defaults`
  set, per-voice sets, server aliases, letter-name and protect overrides.
  Verified at load, reloaded on mtime change, and absent-safe.
- Per-voice lookup: `[voice:…]` mid-script uses that voice's set, and an alias
  inherits the resolved voice's set.
- The normaliser is driven by the locale (`LOCALE`, or the lexicon's
  `locale`); the digit words, year range and letter names come from the locale
  table rather than a per-value environment variable.
- A heteronym sense tag (`[sense:bass=music]`) for every one of the 100 words,
  carried as two marker rules (`word|sense`) in the lexicon.
- Bare-word decision rules: per-sense `before` / `after` cues, ordered
  most-used first. A bare word with no cue nearby is guessed and reported;
  `stress` pairs get a tag only, because no respelling can split them.
- One committed lexicon file (`lexicon/lexicon.json`) is both the source and
  the runtime file: no build step, no separate heteronym table, no CSV.
- A `before` cue defaults to a two-word look-back and an `after` cue to the end
  of the sentence, so context rules need no window; an explicit `within` still
  overrides both sides.
- The sense tag matches the `[voice:]` / `[emotion:]` grammar instead of a bare
  `|` in the prose. The parser desugars it to the internal rule key; an unknown
  sense is a `400`, never guessed over. The tag works in a turn-array `text` too,
  and a literal `|` in prose is left as text rather than treated as a marker.
- `ambiguity` on `POST /v1/audio/speech`: `guess` (default) picks the first
  candidate and reports it in `X-Kokoro-Tone-Guessed`; `ignore` leaves the engine's
  default; `error` returns `400` with `code: ambiguous_heteronym` and an
  `ambiguities` array of word, offset, tag options, respellings and the guess.

### Added

- One structured log line per event on stderr (`event=speech …`,
  `event=error …`, `event=lexicon …`), in `key=value` form for `grep` and
  Loki's `| logfmt`. A speech line carries `passthrough`, `chars`, `turns`,
  `voice`, `fmt`, `normalise`, `guess`, `upstream_ms`, `render_ms` and `bytes`;
  an error line carries the path, status and detail. Request text is never
  logged. The engine is pinned to `API_LOG_LEVEL=INFO` because its DEBUG
  default writes every text chunk verbatim.

### Changed

- Australian spelling in the code: `sanitise_text`, `_synthesised`,
  `audio.render(synthesise=)`, `scripts/analyse.py` and `make analyse`.
- A replacement keeps a sentence-initial capital, so `Wind the clock` becomes
  `Wined the clock`, not `wined the clock`.
- The image compiles its own ffmpeg from a pinned source tarball with
  `--disable-everything`, enabling only the filters and encoders `audio.py`
  calls. Debian's package pulled in 455 MB, ~292 MB of it libraries the audio
  chain never loads (`libllvm15`, `libflite1`, `libmfx1`, `libx265`,
  `librsvg`, `libcodec2`, `libplacebo`). The binary is 2.4 MB and starts in
  2 ms; the image drops from 656 MB to 164 MB. `uv` no longer ships: the venv
  is built in a separate stage and copied in.

### Fixed

- `GET /health` probed `{UPSTREAM_BASE_URL}/health`, which is `/v1/health` for
  the shipped engine. Kokoro-FastAPI serves `/health` at the root and has no
  `/v1/health`, so a healthy engine was reported as `degraded` and the
  container's healthcheck never passed. The probe now asks the root first and
  falls back to `{base}/models`, which is what an OpenAI-shaped upstream (the
  OpenRouter seam) answers. The failure detail names the URL that answered.

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
