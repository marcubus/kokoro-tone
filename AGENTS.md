# AGENTS.md

Guidance for coding agents working in this repository.

## What this is

An OpenAI-compatible speech gateway in front of Kokoro-FastAPI. The product is
the **script layer**: deterministic pronunciation, heteronym, acronym and number
handling that turns a readable script into the text the engine should say. It is
engine-independent and has a standalone CLI. On top sits a small REST surface for
the deployment lexicon and a two-stage pronunciation approval workflow. The
**tone layer** is an experimental lab: the model cannot take direction, so
"emotion" here is measurable prosody - rate, pitch, gain and spectral tilt -
applied with ffmpeg, then mastered once per file. Nothing in this repo claims to
add acting.

The wire format is the contract. `instructions`, `[emotion:…]` and the turn array
are OpenAI-shaped inputs that this gateway translates into DSP. The generated
OpenAPI document is the second contract: routes, roles and every status they
return are tested against it.

## Start here

Read in this order, and stop when the task is covered:

1. `README.md` - what this is, the feature table and the open to-do.
2. `docs/usage.md` - request shapes, tags, the engine subset and the REST surface.
3. `docs/pronunciation.md` - text policy, heteronyms, lexicon and approvals.
4. `docs/audio.md` - pipeline order, presets and the mastering chain.
5. `docs/configuration.md` - environment variables, roles and the allowlist.
6. `docs/development.md` - the module map and the task list.
7. The ADR for the decision you are about to change, then the code.

The docs are the contract; the code and tests enforce it. When a doc and an ADR
disagree, the doc is current and the ADR records why the decision was made.
Accepted ADRs are never rewritten - write a new one and link back.

## Ground rules

- **Filter strings are built from constants only.** `audio._filters` refuses
  anything outside `_FILTER_SAFE`. Never interpolate client text into a filter;
  a new filter must be a constant or a formatted float. `[filter:…]` selects a
  declarative block from `filters.py`; its template is a constant and its
  parameters are typed, bounded numbers. A plugin manifest is data, validated by
  the same schema - never a code path that emits a filter. A plugin name that
  collides with a built-in block is ignored.
- **`ALLOWED_FILTERS` and the `Dockerfile` move together.** A new ffmpeg filter
  needs both the allowlist entry and the matching `--enable-filter`; otherwise
  the request fails at runtime with `No such filter`, not at build time.
- **Presets are pinned by tests.** `tests/test_audio.py` holds `ADR_PRESETS`
  (rate, pitch, gain) and `ADR_FILTERS` (the exact filter tuples). Changing a
  preset means updating those, `docs/audio.md`, ADR-0009 and the changelog in
  the same commit.
- **No per-segment normalisation.** Loudness is one linear gain for the whole
  file, measured after the tone chain. Per-segment `loudnorm` would boost a
  whispered turn back to normal and erase the performance.
- **Pipeline order is fixed:** static voice levelling → per-turn prosody →
  tone chain → single gain → limiter. Levelling first is ADR-0013; swapping it
  makes levelling cancel the preset gain.
- **Tags are independent slots.** `[voice:]`, `[emotion:]` and `[rate:]` do not
  reset each other, and `[emotion:neutral]` clears only the emotion back to the
  request default. Tags are sticky within one `input` string; the turn array has
  no sticky state at all.
- **`instructions` are not parsed semantically.** Word-boundary keyword match,
  longest keyword wins, no negation: "don't whisper" selects `whisper`. Do not
  describe them as understanding the request.
- **A plain string with no tags and no `instructions` is passthrough** and comes
  back unmastered. Only a rendered request is levelled, prosodied, mastered and
  encoded once.
- **The metric surface is bounded.** A metric or log field is an operator
  signal, never a copy of the script: no request text, voice name, alias, client
  address or request id. The token is never logged. Unknown paths are labelled
  `unmatched`, not the raw path.
- **The learned overlay is the only lexicon file written at runtime.** Shipped
  defaults and `custom.json` are immutable through the API; the gateway writes
  `learned.json` (and, in `review` mode, `proposals.json`) and nothing else.

## Access and the pronunciation API

- **Two roles and no more.** A token in `KOKORO_TONE_API_KEY(S)` is `default`; a
  token in `KOKORO_TONE_ADMIN_KEY(S)` is `admin`. The admin list is checked
  first. No keys at all means every route is open; do not add a third role.
- **The permission is the OpenAPI `operationId`, not the path.** A default key
  may call only the ids in `[default].allow`; an admin key may call every id. A
  route added in a later release is therefore admin-only for default keys until
  the policy names it.
- **Decisions are admin-only in code.** `updatePronunciation`,
  `deletePronunciation` and `retrievePronunciationAudio` cannot be granted to a
  default key, and a policy file that names one is rejected at startup.
- **`review` mode never self-approves.** `POST` records a UUIDv7 proposal and
  answers `202`; a pending row has no timeout, applies nothing and is never
  pruned to make room. At `MAX_PROPOSALS`, pending rows alone over the cap are a
  `400` rather than a dropped proposal. `review` with no admin key refuses to
  start. The store is one file and one process by design.
- **An approval is two writes, not a transaction.** The rule is applied first,
  the decision recorded second; re-approving is safe. A decided row is
  immutable (`409 invalid_transition`).
- **Deleting a proposal is not undoing it.** An approved rule lives in
  `learned.json` and is removed by its own `learned-…` id.
- **New routes need the contract test in the same change.** `tests/test_openapi.py`
  compares the document and the route table in both directions: every operation
  needs an `x-kokoro-tone-roles` extension and every status it can return. A
  route without a role or an undocumented status fails the suite.

## Commands

```bash
make test      # pytest
make check     # ruff format --check, ruff lint, mypy, security-check, pin-check
make verify    # check + test, the full local CI set
make fmt       # rewrite formatting
make script FILE=script.txt          # compile to spoken text; no server
make script FILE=script.txt NORMALISE=1
make narrate FILE=script.txt OUT=out.mp3
make analyse FILE=out.mp3
```

`ffmpeg` must be on `PATH`; the audio tests shell out to it. `make check` is not
optional - CI runs it, and `security-check` fails on `eval`, `exec`, `pickle`,
`yaml.load`, `shell=True`, `os.system` and similar in `src/`.

The script CLI is the same compiler the HTTP route uses:
`python -m kokoro_tone.script_cli --request request.json --env-file .env`.
Use `--env-file` when reproducing a deployment's limits and lexicon paths;
`--learned` points at the machine-written overlay; `--list-filters` prints the
filter registry.

## Where the contract lives

| File | Owns |
|---|---|
| `README.md` | what differs from OpenAI, each row pointing at its test |
| `docs/usage.md` | request shapes, markup, tag interactions, REST examples |
| `docs/pronunciation.md` | request rules, the lexicon, heteronyms, normalisation, approvals |
| `docs/audio.md` | presets, tone chain, how a preset lands, passthrough vs rendered |
| `docs/configuration.md` | environment variables, bounds, logs, roles, approval, token rotation |
| `docs/observability.md` | OpenTelemetry instruments, cardinality rules, collector and scrape |
| `docs/plugins.md` | adding a `[filter:]` block with a manifest or entry point |
| `docs/voice-loudness.md` | the per-voice measurements behind `levelling.py` |
| `docs/development.md` | module map, task list, pinned ffmpeg and releasing |
| `docs/diagrams/README.md` | diagram sources, conventions and the registry |
| `docs/samples/index.md` | the emotion sample recordings and their script |
| `lexicon/README.md` | the deployment lexicon's JSON schema |
| `docs/adr/` | decisions and their rejected alternatives |
| `CHANGELOG.md` | user-visible changes; CalVer `YYYY.MM.MICRO`, tag `v…` |

Version lives in exactly one place, `src/kokoro_tone/__init__.py`. The engine
tag lives only in `engine.version`; `make pin-check` enforces both.

## Changing a doc

A doc change is part of the change, not a follow-up. Update the owning doc, the
README table if the contract moved, and the ADR only if the decision moved. If
the flow or the pipeline order changed, update the matching diagram source and
re-render it; the registry is in `docs/diagrams/README.md`.

## Measuring audio

These were learned the hard way. Following them avoids re-deriving them.

- **Tilt is the reliable brightness metric**: slope in dB/oct across the 250 Hz
  to 8 kHz octave bands. The `aspectralstats` centroid is frame-alignment
  sensitive - two files with identical octave bands to 0.1 dB can differ 9% -
  so never rank voices or presets by centroid.
- **Compare like with like.** Measure a preset against neutral for the *same
  voice and prose*, after the master. A preset is a delta on the voice's own
  timbre, not an absolute target.
- **A plain string with no tags and no `instructions` is passthrough** and comes
  back unmastered. To get a mastered neutral baseline, send
  `[emotion:neutral] <prose>`.
- **Shelf gain is asymptotic.** `treble=g=5:f=6000` arrives as +2.4 dB in the
  8 kHz octave band, not +5. Roughly 0.07 dB/oct of tilt per dB of shelf.
- **Tilt moves from both ends.** A low-mid cut is about a third of the effect
  and lets the treble shelf stay smaller, which is what keeps sibilants in
  check.
- **Pitch: measure with a whole-file LTAS cepstrum**, not a per-frame median -
  the per-frame estimator is biased low and once produced a phantom 0.5 st
  shortfall. `rubberband` is exact on a pure tone.

## Settled questions - do not reopen without new measurements

- **Pitch delivery is correct** (within ±0.26 st, no directional bias). The
  reported shortfall was an estimator artefact; there is no pitch bug to fix.
- **No per-voice preset table.** The same +5 dB shelf moves 13 male voices by
  +0.40…+0.67 dB/oct. Equalising them would take ~32 dB of treble, which is a
  repair, not a preset. Per-voice correction, if ever wanted, belongs in the
  master's de-harshing and must stay bounded.
- **A voice's spectral character is not a defect.** Do not EQ a dark voice
  bright in the master; `docs/voice-loudness.md` §4 argues this at length.

## Calibration status

Preset calibration is an open to-do in `README.md` § To-do. Every preset except
`neutral` is experimental; `bright` was calibrated but is still not right. Do not
present any preset as validated.

## Deployment

The compose file, environment and start commands live in this repo.

```bash
cp .env.example .env   # set KOKORO_FASTAPI_API_KEY and KOKORO_TONE_API_KEY
make up                # build the gateway, pull the pinned engine, start both
make up-image          # start the released image instead (KOKORO_TONE_VERSION)
make ps                # container state and health
make logs              # follow both logs
```

`make up` reads `engine.version` and passes it to compose as `KOKORO_VERSION`.
The gateway publishes no host port by default, so reach `/health` from another
container on the compose network or add a port mapping locally.

Then check `/health` on the gateway: it reports `version`, the enabled features,
the filter names, and `normalise.pending_proposals`. A stale image reports an
older `version` and omits newer blocks, so `/health` is the fastest way to tell
whether the build you just started is the one you meant to.

The deployment policy is a TOML allowlist at `KOKORO_TONE_RBAC`
(`/etc/kokoro-tone/rbac.toml`); the shipped example is
`lexicon/rbac.example.toml`. With no file, the built-in policy grants every
operation that exists today. An unknown table, key, operation id or version
stops the process at startup. The policy and the code must be deployed together.
