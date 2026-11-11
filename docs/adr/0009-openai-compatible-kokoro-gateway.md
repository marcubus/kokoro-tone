# Serve Kokoro behind an OpenAI-compatible warmth gateway

**Status:** accepted (implemented - [`../../README.md`](../../README.md) is the current contract)
**Date:** 2026-11-10
**Author:** sys-admin
**Provenance:** written in the deployment repo before implementation. Host
details, service wiring and deployment paths have been removed; the module names
below are the ones this repository ended up with.

## Context

What is missing from a bare Kokoro endpoint is a way to turn a **transcript**
into a **multi-speaker narration with a warm, mastered tone** - the thing you
want when a script has a narrator and two characters, and the output is a file
rather than a chat reply.

Kokoro can already do most of that. `remsky/Kokoro-FastAPI` (v0.10.0, 2026-11-08,
Apache-2.0) ships:

- `[voice:...]` inline speaker switching, `POST /dev/dialogue` structured turns,
  voice aliases, and weighted voice blending (`af_bella(2)+af_sky(1)`)
- `[pause:1.5s]`, `[rate:1.5]`, IPA pronunciation tags, and an SSML subset
  (`<break>`, `<voice>`, `<prosody rate>`, `<phoneme>`, `<sub>`)
- sentence-boundary chunking, streaming, six output formats, word timestamps
- an OpenAI-compatible `POST /v1/audio/speech`

What it does not ship is the part this ADR is about:

- **`instructions` is silently dropped.** `OpenAISpeechRequest` inherits a plain
  Pydantic model with no `extra="forbid"`, so an OpenAI client that sends
  `instructions: "speak warmly"` gets a 200 and no effect.
- **`<prosody pitch>` and `volume` are discarded** - only `rate` survives.
- **No tonal mastering.** The output is dry Kokoro: no low-shelf, no de-harshing,
  no compression, no loudness target.

Kokoro-82M is 82M parameters and runs on CPU at roughly 1 GB resident - an order
of magnitude below a general speech stack. That is what makes the scope
defensible: a single small model plus a stateless shim, not a speech platform.

The consumer is batch narration: an OpenAI-shaped request from a script, an
editor plugin, or `curl`, with the whole result returned as one file.

## Decision

Run **two containers** and keep the OpenAI contract in the middle.

```
clients ──POST /v1/audio/speech──▶ kokoro-tone (FastAPI + ffmpeg, no model)
                                     │  parse markup, map instructions
                                     ▼
                                   kokoro (Kokoro-FastAPI, Kokoro-82M)
                                     │  PCM back
                                     ▼
                                   kokoro-tone: prosody → warmth → loudness → encode
```

- **`kokoro`** - `ghcr.io/remsky/kokoro-fastapi-cpu:v0.10.0`, internal only,
  `API_KEYS` set, no published ports, `mem_limit: 2g`. It owns inference, voices,
  blending, aliases, SSML translation, chunking, formats and timestamps. We do not
  patch it.
- **`kokoro-tone`** - a small FastAPI service with `ffmpeg` and no model,
  `mem_limit: 512m`, `cap_drop: [ALL]`, no state. It owns the OpenAI request
  shape, `instructions`, per-turn prosody, the warmth chain and loudness.

Both live in one `compose.yml` as a multi-service file, ordered `kokoro` then
`kokoro-tone`, with `depends_on: condition: service_healthy`.

## Why local inference, not a hosted API

A hosted Kokoro is cheaper than running anything locally (about $0.62 per million
characters) and would remove the 82M model from the machine entirely. It was
rejected because it loses exactly the capabilities this decision is for:

- **No multi-speaker input.** OpenRouter supports `input` as a list of turns only
  for Gemini TTS and Fish Audio; Kokoro returns a 400. We would have to split and
  stitch ourselves anyway.
- **No voice blending or aliases.** OpenRouter exposes 54 fixed presets.
- **No SSML, no timestamps, no streaming.**
- **`instructions` is ignored** by Kokoro upstream as well.

So the proxy design would reimplement turn splitting and still deliver less. The
local engine is 82M parameters, ~1 GB resident. The hosted engine stays available
as a fallback by changing `UPSTREAM_BASE_URL`, and is documented in
[`../configuration.md`](../configuration.md) rather than wired in by default.

## Why a gateway and not a fork

The gateway is a reverse proxy with an audio chain. It does not load Kokoro, does
not copy Kokoro-FastAPI code, and speaks only its public HTTP API. Upstream shipped
v0.10.0 the day before this was written; a fork would carry that churn every week.
The gateway also keeps the engine swappable: a different `UPSTREAM_BASE_URL` is a
different engine, with no client change.

## What "emotion" means here

**Prosody and mastering, not acting.** This belongs in the record because the
distinction decides whether the service is worth having.

Kokoro's glottal source is fixed and neutral. A filter can move the average pitch,
the rate, the loudness and the spectral tilt. It cannot add breathiness, creak, a
smile, or F0 *range* within a sentence - those are source properties. So
`[emotion:sad]` means "slower, lower, darker", not "sad". A listener will describe
the result as a different tone, not a performance.

What the chain does buy, and what a listener will hear:

| Layer | Effect | Honest description |
|---|---|---|
| Casting | distinct voices per speaker, blends available | "two different people" |
| Pacing | rate, pauses, inter-turn gaps | "not robotic" |
| Warmth | low-shelf, de-harshing, compression, saturation | "nice, close, not thin" |
| Loudness | −16 LUFS, −1 dBTP | "a deliverable, not a test file" |
| Emotion presets | ±3 semitones, ±20% rate, ±6 dB | "a different mood" |

## Considered options

**Kokoro-FastAPI alone.** Rejected: it gives multi-speaker, blending, SSML and
timestamps, but `instructions` is dropped and there is no pitch or mastering. That
is the entire gap.

**`fastkokoro` (ONNX, PyPI).** Lighter and faster than the PyTorch image, but no
markup, no SSML, no captions, and no turn handling. It would push all parsing into
the gateway and still need a second engine for anything beyond plain text.

**Fork Kokoro-FastAPI and add pitch/volume to its SSML.** One container, no hop.
Rejected: merge burden against a project shipping weekly, and the fork would have to
carry the same `instructions` and mastering work anyway.

**`Xerophayze/TTS-Story`.** The closest workflow match: tagged scripts
(`[narrator]…[/narrator]`), Kokoro on CPU, per-speaker pitch/speed, pause markers,
chunk-level effects, ACX-oriented processing, M4B/MP3 export. Rejected because it is
a Flask app with a browser UI, not an OpenAI endpoint - it consumes OpenAI-compatible
TTS rather than exposing it. Adopting it would mean running a monolith to get an
API it does not have.

**`mcp-tool-shop-org/audiobooker` (MIT, Docker).** EPUB → multi-voice audiobook with
emotion inference and real ACX mastering (two-pass `loudnorm` at −20 LUFS, peak, noise
floor). Rejected as the service, but its mastering targets and its "measure the noise
floor, never gate it" rule are adopted here.

**`nodeblackbox/Kokoro-Voice-Api`.** A Kokoro API that already exposes
`effects: {pitch: {semitone_shift, preserve_formants}, compression, reverb}`. The
closest existing warmth chain. Rejected as the base: separate Flask server, no
`[voice:]`, no SSML, no multi-speaker, last push Sep 2025. Its effect parameter shape
is copied.

**OmniVoice, CosyVoice3, IndexTTS-2.5, Chatterbox, Orpheus, Higgs, VibeVoice,
MOSS-TTSD, vLLM-Omni.** Rejected for this project: not Kokoro (the chosen engine), or
GPU-only, or too large for the target machine (CosyVoice3 weights are 9.75 GB), or a
hobby wrapper (0–19★), or retired (Chatterbox, Aug 2026). IndexTTS-2.5 is the
documented upgrade path if acted emotion is ever required - it needs a GPU.

**Hosted Gemini TTS with native turns.** Zero code, real emotion, per-turn
`voice` and `instructions` in one request. Rejected: not Kokoro, higher per-character
cost, and text leaves the machine. Worth revisiting if the API surface matters more
than the engine.

**Piper / `openedai-speech`, cloud SSML TTS, writing our own inference server.**
Rejected: wrong voices or no markup; external dependency; reimplements chunking,
streaming and formats that Kokoro-FastAPI already gets right.

**Nix-built image.** Rejected for v1. Nix would give a byte-reproducible image, but
the image is 90% ffmpeg either way, and a locally built image has to be rebuilt on
every machine that runs it. A `flake.nix` devShell remains an option; a NixOS
migration is a platform rewrite, not a service decision.

## Consequences

**We own the gateway and its tests.** It is small, but it is code that must be
maintained, unlike the pinned engine image.

**Emotion is prosody.** Documented above; `[emotion:x]` is a tone preset, not acting.

**No per-turn pitch on the passthrough path.** When a request has no tags and no
`instructions`, the gateway forwards it whole (streaming preserved). Per-speaker
pitch requires splitting, which costs a Kokoro call per turn and returns a complete
file rather than a stream. Batch use is unaffected.

**Double encoding is avoided.** Kokoro returns PCM; the gateway processes PCM and
encodes once at the end.

**Loudness must not be normalised per segment.** A single linear gain is applied to
the whole file after measurement. Per-segment `loudnorm` would boost a `[emotion:whisper]`
line back to normal and erase the performance. This is the trap `audiobooker`
documents and the reason its `master-check` measures rather than corrects. The
consequence is that the master cannot equalise *voices* that sit at different
levels - that is a separate, static correction, measured in
[`../voice-loudness.md`](../voice-loudness.md).

**Text never leaves the machine.** The whole stack is local. The hosted fallback
is opt-in and documented, not default.

**Two containers, not one.** Kokoro is 82M and ~1 GB; the gateway is a few hundred
megabytes. That is an order of magnitude below a general speech stack, but it is
still two processes, and it is why the model is pinned to a release tag rather
than `latest`.

## Design

### Topology

```
compose.yml
  kokoro        ghcr.io/remsky/kokoro-fastapi-cpu:v0.10.0   internal, API_KEYS, 2g
  kokoro-tone   build: .                                    internal, bearer, 512m
```

Neither publishes a port. `kokoro-tone` reaches `kokoro` at
`http://kokoro:8880/v1`. Routing the gateway to a browser is a deployment choice,
not part of this decision.

### Endpoints

| Method | Path | Notes |
|---|---|---|
| POST | `/v1/audio/speech` | OpenAI shape: `model`, `input`, `voice`, `instructions`, `response_format`, `speed` |
| GET | `/v1/models` | `kokoro`, `tts-1`, `tts-1-hd` aliases; `gpt-4o-mini-tts` rejected |
| GET | `/v1/audio/voices` | proxied from Kokoro, cached |
| GET | `/health` | unauthenticated; checks upstream reachability |

### Request shape

`input` is either a string with markup or an array of turns (the OpenRouter shape):

```json
{"input": [{"text": "Did it land?", "voice": "af_bella", "instructions": "anxious"},
           {"text": "It did.", "voice": "am_michael"}]}
```

Markup accepted inside a string:

```
[voice:af_heart] [emotion:warm] The rain had stopped by morning.
[voice:am_michael] [emotion:tired] Did anyone check the gauge?
[pause:1.2s]
[voice:bf_emma] I did - it's over the wall.
```

`[voice:]` accepts anything Kokoro accepts, including blends and aliases.
`[rate:]` and `[pause:]` keep Kokoro's semantics. `[emotion:]` is ours.

### `instructions` → prosody

Free text is matched against a small keyword table; unmatched text falls back to
`speed` and the default warmth profile. `[emotion:x]` overrides the request-level
mapping for that turn.

| Preset | rate | pitch | gain | EQ |
|---|---|---|---|---|
| warm | ×0.95 | −1 st | +1 dB | low-shelf +1.5 dB @ 250 Hz |
| bright | ×1.05 | +1 st | +1 dB | high-shelf +1.5 dB @ 6 kHz |
| sad | ×0.88 | −2 st | −3 dB | lowpass 6 kHz |
| tired | ×0.90 | −1 st | −2 dB | lowpass 8 kHz |
| angry | ×1.15 | +1 st | +4 dB | treble +2 dB, compression 3:1 |
| whisper | ×0.85 | 0 | −8 dB | highpass 300 Hz, lowpass 8 kHz |
| excited | ×1.12 | +2 st | +2 dB | treble +3 dB |

Pitch is `rubberband` with formant preservation; the fallback is `asetrate` +
`atempo`, which shifts formants and is only used if `rubberband` is unavailable.

### Warmth master (whole file, once)

```
low-shelf  +2.0 dB @ 250 Hz
high-shelf −1.5 dB @ 6 kHz
acompressor ratio 2.5, attack 15 ms, release 200 ms
tanh saturation at low drive
alimiter −1 dBTP
single linear gain → −16 LUFS (LOUDNESS_LUFS; −20 for ACX-style audiobooks)
```

No reverb. No de-esser in v1. No per-segment loudness.

### Deferred to v1.1

A **performance layer** that derives prosody from the text itself: `.` lowers pitch
slightly, `?` rises on the final clause, `…` inserts a pause, long sentences speed
up, ALL CAPS gets emphasis before the normaliser strips it. This is where perceived
emotion actually comes from, but it needs listening tests and a corpus to tune
against. v1 ships presets and pauses only.

### Environment

| Variable | Default | Purpose |
|---|---|---|
| `UPSTREAM_BASE_URL` | `http://kokoro:8880/v1` | engine seam; a hosted engine is a supported alternative |
| `UPSTREAM_API_KEY` | - | Kokoro `API_KEYS` entry, or the hosted key |
| `UPSTREAM_MODEL` | `kokoro` | `hexgrad/kokoro-82m` when pointed at a hosted engine |
| `KOKORO_TONE_API_KEY` | - | bearer required by the gateway; empty disables auth |
| `LOUDNESS_LUFS` | `-16` | mastering target |
| `TURN_GAP_MS` | `350` | silence between turns without an explicit pause |
| `MAX_INPUT_CHARS` | `4096` | OpenAI parity |
| `SEGMENT_CHARS` | `1000` | split threshold for long turns |

*Names as first written.* The shipped names are `KOKORO_TONE_*` and
`KOKORO_FASTAPI_*`; see [`../configuration.md`](../configuration.md).

### Auth

A bearer token compared with `secrets.compare_digest`. `/health` is open;
everything else is 401 without it. The upstream key never leaves the container.

## Implementation

- `src/kokoro_tone/main.py` - routes, auth, upstream client, error mapping (400/401/502).
- `src/kokoro_tone/parse.py` - turns and markup parser; rejects unbalanced tags.
- `src/kokoro_tone/prosody.py` - `instructions` keyword table, emotion presets,
  rate/pitch clamps (±3 st, 0.75–1.3 rate).
- `src/kokoro_tone/audio.py` - ffmpeg chain, PCM handling, silence insertion,
  concat, single-gain loudness, encode.
- `tests/` - parser, prosody mapping, and a golden-audio test asserting duration,
  sample rate, loudness and true peak.
- `scripts/narrate.py` - transcript file → mp3 via the gateway.
- `scripts/analyse.py` - mean F0, syllables/s, spectral centroid, LUFS, for A/B of
  presets.

## Acceptance criteria

- `POST /v1/audio/speech` with three `[voice:]` turns returns one file whose duration
  is the sum of the turns plus the pauses.
- The same request with `instructions: "sad"` and `instructions: "angry"` differs in
  mean F0 and syllables/second in the expected direction (`scripts/analyse.py`).
- Output lands at −16 LUFS ±1 with true peak ≤ −1 dBTP.
- An unmodified OpenAI Python client (`client.audio.speech.create`) works against
  the gateway.
- A request without `Authorization` returns 401; `/health` returns 200.
- `kokoro` has no published port.
- A 500-character single-voice request renders faster than 1.5× realtime on a
  six-core desktop CPU.
