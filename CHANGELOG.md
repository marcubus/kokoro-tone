# Changelog

All notable changes to `kokoro-tone` are recorded here.

Versions use CalVer in the form `YYYY.MM.MICRO`. `MICRO` starts at `0` each
month and increments per release; tags are prefixed with `v`.

## [Unreleased]

### Documentation

- Clarified how the tags interact, in [`docs/usage.md`](docs/usage.md),
  [`docs/audio.md`](docs/audio.md) and ADR-0009: `[voice:]` resets neither the
  emotion nor the rate, so a tag set for one speaker follows the next;
  `instructions` set a file-wide tone that `[emotion:]` overrides per turn and
  `[emotion:neutral]` returns to; and a preset is applied before the tone chain,
  so it is a delta on the voice's own timbre and the chain's shelves stack with
  it.

## [2026.11.2] - 2026-11-11

### Added

- `[emotion:neutral]` clears a sticky emotion tag instead of selecting a preset.
  Tags apply until changed, so a script had no way back to the request default;
  `neutral` removes the turn-level override, after which the request's
  `instructions` apply as usual.
- Static per-voice loudness levelling. Every turn is given the casting gain
  measured in [`docs/voice-loudness.md`](docs/voice-loudness.md) for its voice
  (all 72 measured voices), passed to the engine as `volume_multiplier` so it
  applies on both the mastered and passthrough paths. Blends interpolate their
  members' gains by weight, OpenAI voice names resolve to their measured Kokoro
  voice, and an unmeasured voice stays at unity. `VOICE_LEVELLING=false` turns it
  off; `GET /health` reports the table size.
- [ADR-0013](docs/adr/0013-level-voice-before-emotion.md) records the pipeline
  order: static voice levelling runs before the per-turn emotion preset, so a
  whisper stays quiet and an angry line stays hot.

### Changed

- Recalibrated the `bright`, `warm` and `excited` presets against measured
  post-master spectral tilt. The tone chain's −1.5 dB shelf at 6 kHz was taking
  back most of any treble boost, so `bright` delivered −0.05 dB/oct (no brighter
  than neutral) and `excited` +0.04 despite +3 dB of shelf. Tilt now moves from
  both ends - a treble shelf up and a low-mid shelf down - because the cut is
  about a third of the effect and lets the shelf stay 2.5 dB smaller, which
  keeps sibilants in check. Measured deltas against neutral: `bright` +0.36
  dB/oct and +1.3 dB of HF, `excited` +0.72 and +2.9, `warm` −0.31 and −1.6
  (with its bass shelf halved, since it stacked with the tone chain's +2 dB).
  `sad`, `tired`, `angry` and `whisper` are unchanged - they use filters, which
  the tone chain does not cancel, and already delivered their intent.
- One constant per preset, not a per-voice table. The same +5 dB shelf moves 13
  male voices by +0.40…+0.67 dB/oct (mean +0.52, sd 0.09); closing the 2.6
  dB/oct spread between the darkest and brightest voice would take roughly 32 dB
  of treble, so per-voice preset tuning was measured and rejected.
- Renamed the whole-file warmth master to the **tone chain**:
  `WARMTH_CHAIN`/`apply_warmth` are now `TONE_CHAIN`/`apply_tone`. The emotion
  preset is still `warm`, so the two are no longer easy to confuse.

### Fixed

- `docs/audio.md` and ADR-0009 claimed a −1 dBTP ceiling; `alimiter=limit=0.891`
  is −1 dBFS *sample* peak, which measures −0.47…−0.74 dBTP. Both now say so.
- `docs/voice-loudness.md` treated the spectral centroid as the brightness
  metric. It is frame-alignment sensitive (two files with identical octave bands
  to 0.1 dB differed by 9%) and ranks voices differently from tilt, so the
  ordering is now marked approximate and tilt is named as the reliable measure.

### Notes

- Downward pitch was reported as short by about 0.5 st. It is not: a pure tone
  through `rubberband` is exact, and a whole-file LTAS cepstrum puts delivery
  within ±0.26 st of the request in both directions. The shortfall came from a
  per-frame median estimator that is biased low. No pitch code was changed, and
  the test that measures pitch now isolates it from the preset EQ, whose large
  treble shelf was inflating the zero-crossing count.

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
