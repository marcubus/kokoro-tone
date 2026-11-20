# Changelog

All notable changes to `kokoro-tone` are recorded here.

Versions use CalVer in the form `YYYY.MM.MICRO`. `MICRO` starts at `0` each
month and increments per release; tags are prefixed with `v`.

## [Unreleased]

### Added

- **Two roles and a TOML allowlist (ADR-0016).** `KOKORO_TONE_ADMIN_KEY(S)` is
  the new `admin` role; `KOKORO_TONE_API_KEY(S)` remains `default`. Every route
  carries an OpenAPI `operationId`, and that id is the permission unit. A
  default key may call only the ids in `[default].allow`; an admin key may call
  every operation, including one no list names. The policy lives at
  `KOKORO_TONE_RBAC` (default `/etc/kokoro-tone/rbac.toml`);
  `lexicon/rbac.example.toml` is generated from the built-in defaults. A
  missing file uses the built-in policy, which grants every operation a default
  key may call, so an upgrade with keys and no file changes no behaviour. An
  unknown `version`, table or operation id stops the process at startup.
- **A denied operation is a scoped 403.** A recognised key on a slug its role
  may not call returns `permission_error` / `insufficient_scope` with
  `WWW-Authenticate: Bearer error="insufficient_scope"`. Authentication is
  checked first, so a default key on a denied slug is 403, never 401. Every
  authentication failure remains the same uniform 401, and the reason is logged
  with the path and slug only.
- `GET /health` reports an `access` block: whether auth is enabled, whether the
  policy came from a file or the built-in defaults, and whether each key list is
  populated. No key material is reported.
- **Two-stage pronunciation approval (ADR-0017).** `PRONUNCIATION_APPROVAL`
  chooses `force` (default, today's behaviour) or `review`. In `review`, `POST
  /v1/pronunciations` records a proposal with a UUIDv7 id and answers `202`;
  nothing enters the learned overlay until an admin key approves it with
  `PATCH /v1/pronunciations/{id}`. A rejection keeps the row and its reason.
  `GET /v1/pronunciations/{id}/audio?variant=as-is|proposed` renders the
  proposal's own context sentence twice, so the decision is made on sound.
  An uppercase run in `say` is a `400` in review and a logged warning in force.
  `review` with no admin key refuses to start; an unknown mode stops the
  process. The proposal store is a second JSON file beside `learned.json`
  (`PRONUNCIATION_PROPOSALS`), with its own atomic write, byte cap and symlink
  refusal.
- **The deciding operations are admin-only in code.** `updatePronunciation`,
  `deletePronunciation` and `retrievePronunciationAudio` are never in the
  built-in default policy, and a policy file that names one is rejected at
  startup. A default key may propose; it may not decide.
- **The OpenAPI document is the contract (ADR-0018).** `GET /openapi.json` is
  generated from the route models and carries an `x-kokoro-tone-roles`
  extension on every operation (`public`, `admin`, or `default` and `admin`),
  plus the OpenAI error envelope for every status the operation can return.
  `tests/test_openapi.py` compares the document with the route table in both
  directions, so a route without a role, a status or a schema fails the suite.
  FastAPI's unused 422 is removed: validation failures are the same 400
  envelope as every other failure.
- **The dry run is the speech route.** `POST /v1/audio/speech?dry_run=1`
  returns the render's own prepared view (`text`, `turns`, `guesses`,
  `ambiguities`, `normalise`) as JSON, with no engine call and no audio.

### Breaking (ADR-0016)

- **`PRONUNCIATION_WRITE_KEY` and the `X-Pronunciation-Key` header are
  removed.** A second secret proved possession without saying which operations
  it unlocked, so it could not express "may propose but not approve". The write
  routes are now granted by operation id like every other route:
  `createPronunciation` to propose or apply, `deletePronunciation` to delete.
  `PRONUNCIATION_WRITE` still decides whether a force-mode write may happen at
  all. `deletePronunciation` is now admin-only (ADR-0017), so a default key that
  could forget a word before the change no longer can.
- **A new operation id is admin-only for a default key** until a policy file
  names it. The built-in policy is an explicit list, so adding a route does not
  silently widen what an episode renderer may call.
### Breaking (ADR-0018)

- **The pronunciation verb routes are removed without aliases.**
  `POST /v1/pronunciations/lookup` becomes `GET
  /v1/pronunciations?word=<word>` (repeatable); `POST
  /v1/pronunciations/learn` becomes `POST /v1/pronunciations` (which answers
  `201` applied or `202` queued); `POST /v1/pronunciations/forget` becomes
  `DELETE /v1/pronunciations/{id}`, and the id comes from the create response or
  the list. The old paths no longer run the old handlers.
- **`POST /v1/script/compile` is removed.** The dry run is
  `POST /v1/audio/speech?dry_run=1`: the same `SpeechRequest` body, the same
  `pipeline.prepare` result, no audio. The `compileScript` operation id is gone
  from the route table, the policy vocabulary and the generated
  `rbac.example.toml`; a policy file that still names it refuses to start.
- **List responses move to one envelope:** `{"object": "list", "data": [...]}`,
  replacing `{"results": [...]}`. A single row is `{"object":
  "pronunciation", ...}` or `{"object": "proposal", ...}`.
- **State conflicts are `409` with a code**, not a generic failure:
  `invalid_transition`, `not_pending`, `immutable_rule`.
- The base OpenAI surface (`createSpeech`, `listModels`) is unchanged.

### Fixed

- `separate` no longer gets a hyphenated respelling that Kokoro reads with a
  stress on every syllable and the wrong vowel. The adjective passes through as
  `separate` (the engine reads `sˈɛpəɹət`), and the verb respells as `separayt`
  (`sˈɛpəɹˌAt`). This covers both the `[sense:separate=…]` tags and the bare
  context rules.

## [2026.11.4] - 2026-11-17

### Added

- `POST /v1/script/compile`: the render's own `pipeline.prepare` step, returned
  as JSON with no engine and no audio. The body carries `text`, `turns`,
  `guesses` (the `word=sense` list the compiler assumed, empty when nothing was
  guessed), `ambiguities` (the same assumptions with their alternatives) and
  `normalise` (the layers that ran). `X-Kokoro-Tone-Guessed` carries the compact
  list, so a caller can refuse a request in milliseconds instead of paying for
  a synthesis and reading the header afterwards.
- `script_cli --request FILE` compiles a whole speech request (input, voice,
  pronunciations, normalise, ambiguity) through the same request shape the HTTP
  routes accept, and prints the same JSON the dry run returns.
- `script_cli --env-file FILE` reads the deployment's `MAX_*`,
  `PRONUNCIATION_*`, `NORMALISE_*` and `LOCALE` keys without overriding an
  explicit environment. The limits are deployment configuration: the code
  default of 4096 input characters rejects a request the deployed gateway
  accepts at 60000, so an offline check without the env file disagreed with the
  server.
- `script_cli --learned FILE`, and the CLI now composes the learned overlay at
  all. It previously built `Lexicon` without `learned_path`, so a reading the
  gateway had learned was invisible offline.

### Changed

- `Compiled.as_dict()`, `Prepared.as_dict()` and the CLI share one `script.view`
  builder, so a field added to the compiled view reaches the library, the CLI
  and both HTTP routes at once.

## [2026.11.3] - 2026-11-17

### Added

- A runtime pronunciation API: `POST /v1/pronunciations/lookup` returns every
  deployment rule for a word, including heteronym `senses` and the source
  layer; `/learn` and `/forget` write a separate machine-owned learned overlay
  when `PRONUNCIATION_WRITE=true`. Priority is user overlay, learned overlay,
  then shipped defaults.
- Write hardening for the pronunciation API: words and respellings accept only a
  small character set, senses are short labels, the learned file may not be a
  symlink, and writes are capped by `lexicon_rules` and `lexicon_bytes`.
  `PRONUNCIATION_WRITE_KEY` adds an `X-Pronunciation-Key` gate on `/learn` and
  `/forget`.
- A standalone script compiler: `script.py` exposes `ScriptInput` →
  `Compiled`, and `python -m kokoro_tone.script_cli` (or `make script`) resolves
  pronunciation, heteronyms, acronyms and numbers without a server or the
  engine. `pipeline.prepare` is now a thin adapter over the same facade.
  `compile_text()` needs no settings, and `Compiled.text`, `as_dict()` and
  `to_json()` make the result directly consumable.
- Filter blocks can be added with a JSON manifest through
  `KOKORO_TONE_FILTER_MANIFESTS`, without packaging a distribution. Entry-point
  manifests still work; local files load last and win. See
  [`docs/plugins.md`](docs/plugins.md).
- `python -m kokoro_tone.script_cli --list-filters` prints every block, its
  template and its parameter bounds, so the `[filter:]` surface is discoverable.
- The script CLI accepts `--ambiguity error` and returns exit code `4` for an
  unresolved heteronym, distinct from `3` for an invalid script.
- A declarative filter registry and the `[filter:block key=value ...]` tag.
  Blocks are constant ffmpeg templates with typed, bounded numeric parameters;
  the rendered expression is still checked against the filter allowlist and the
  constants-only grammar. `[filter:none]` clears a sticky filter.
- Declarative plugin manifests through the `kokoro_tone.filters` entry point.
  The payload is validated by the same schema as the built-in blocks; a bad
  manifest is skipped with a warning.
- `ambiguity: "error"` now does what the docs always said: it returns a `400`
  with `code: "ambiguous_heteronym"` and the pasteable `[sense:…]` options,
  instead of being rejected as an unknown mode.
- OpenTelemetry metrics exported over OTLP/HTTP to a collector: HTTP server
  metrics, speech work and render cost, upstream latency and health, ffmpeg
  stages, mastering gain and peak, and lexicon problems. The gateway exposes no
  `/metrics` route; the collector owns the Prometheus endpoint. See
  [`docs/observability.md`](docs/observability.md).
- Publish signed, scanned `linux/amd64` and `linux/arm64` images to
  `ghcr.io/marcubus/kokoro-tone` on a `v*` tag: full version, `YYYY.MM` line and
  `latest` tags, a Trivy scan, and a keyless cosign signature. The GitHub
  release is only created after the image job succeeds.
- Add `compose.prod.yml`, `make up-image` and `make pull` to run the released
  image without a local build.
- Ship `docker/THIRD_PARTY_NOTICES.md` in the image, covering the GPL-licensed
  FFmpeg and Rubber Band binaries.

### Changed

- Split pure text and voice shaping into `text.py`, leaving `request.py` with
  only the OpenAI body and model catalog. `script.py` no longer depends on the
  request model, and `parse.py` no longer imports the audio filter registry:
  the script layer carries `[filter:]` as written and the audio layer validates
  it.
- `filters.registry()` now returns a read-only mapping, so no caller can mutate
  the process-wide block registry.
- Repositioned the project around the script layer: the README, package
  description and `AGENTS.md` now lead with pronunciation, heteronyms and
  normalisation, and present the prosody presets as an experimental lab. See
  [ADR-0015](docs/adr/0015-script-layer-is-the-product.md).
- Drop `pip` from the runtime image, since nothing installs Python packages
  after the build stage, and split the CI Trivy scan into a full-severity SARIF
  upload plus a separate `HIGH,CRITICAL` gate. The SARIF mode ignores the
  severity filter, so medium-severity findings were failing the build.
- Marked every prosody preset except `neutral` as experimental. `bright` was
  calibrated against measurement but still does not sound right, so no preset
  is presented as validated.
- Added a README to-do for preset calibration and the known `sad`/`whisper`
  work.
- Re-rendered the emotion samples from the live container with a one-second
  pause and neutral labels, so only the sentence after each pause carries the
  emotion.
- Moved the upstream HTTP client from `httpx` to `httpx2`, the maintained
  successor. Starlette's test client already prefers `httpx2`, so the test
  stack no longer installs two HTTP clients.
- Split the gateway into a composition root and a pipeline. `main.create_app`
  builds one `Context` (settings, lexicon, upstream client) per app; routes take
  it through a dependency, `pipeline.py` owns the order of operations, and no
  module reads settings or opens an HTTP client at import time.
- Replaced `print(..., file=sys.stderr)` with `logging`. The logfmt `event=`
  shape is unchanged; `LOG_LEVEL` now controls the gateway, and an unhandled
  500 logs a traceback.
- Added `make type-check` (mypy) to `make check`, and `make verify` for the
  full local CI set.
- Documented and set `API_LOG_LEVEL=INFO` on the engine, so its DEBUG text
  chunks (the script) are not logged.

### Documentation

- `docs/configuration.md` now lists `PRONUNCIATION_LEARNED`,
  `PRONUNCIATION_WRITE` and `PRONUNCIATION_WRITE_KEY`, and documents the lexicon
  write log events.
- Added four Excalidraw diagrams - request lifecycle, audio pipeline,
  pronunciation resolution and observability pipelines - embedded in the docs,
  with the source files and a registry under [`docs/diagrams/`](docs/diagrams/README.md).
- Added a "Start here" reading order and a fuller document map to `AGENTS.md`,
  and filled the missing pronunciation, development, diagram, sample and lexicon
  entries in the README documentation table.
- Fixed the sample page's link to the README to-do.
- Clarified how the tags interact, in [`docs/usage.md`](docs/usage.md),
  [`docs/audio.md`](docs/audio.md) and ADR-0009: `[voice:]` resets neither the
  emotion nor the rate, so a tag set for one speaker follows the next;
  `instructions` set a file-wide tone that `[emotion:]` overrides per turn and
  `[emotion:neutral]` returns to; and a preset is applied before the tone chain,
  so it is a delta on the voice's own timbre and the chain's shelves stack with
  it.

### Fixed

- A bad `[filter:]` on a later turn is now rejected in `pipeline.prepare`,
  before the first synthesis, instead of after earlier turns had already been
  rendered. The request is still a `400`.
- A filter manifest can no longer shadow a built-in block. A name that collides
  with a shipped block is ignored with a warning, so a deployment cannot
  silently change what `[filter:lowpass]` means.
- `/v1/pronunciations/lookup` now reports rules from per-voice sets, tagged with
  the `voice` they apply to, instead of only the `defaults` rules.
- The lexicon write events (`pronunciation_learn`, `pronunciation_forget`) log
  the word's length, the sense label and rule counts, never the word itself.
- `load_settings` warns when `PRONUNCIATION_WRITE=true` with neither a gateway
  key nor `PRONUNCIATION_WRITE_KEY`, since the write endpoints would then be
  unauthenticated.
- Decided the Morning Glory heteronyms that were being guessed wrong: `record`
  after a noun cue such as `finance` is the noun, `separate` after a number or
  determiner is the adjective, and `live` before a noun is the adjective.
  Corrected `Terraform` to `ter-uh-form`. Added regression coverage in
  `tests/test_lexicon.py`.

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
