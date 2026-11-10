# kokoro-tone

[![ci](https://github.com/marcubus/kokoro-tone/actions/workflows/ci.yml/badge.svg)](https://github.com/marcubus/kokoro-tone/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.12-blue.svg)](pyproject.toml)

An OpenAI-compatible speech endpoint for a small, local TTS model — built on
[Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI), with the prosody and
mastering that make it feel closer to a much larger one.

> **Unofficial hobby project.** This is an independent wrapper. It is not
> affiliated with, endorsed by, or supported by the Kokoro-FastAPI maintainers,
> hexgrad, or OpenAI. Engine and model issues belong upstream; gateway issues
> belong here.

## Why this exists

[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M) is tiny, fast and
CPU-friendly. It is also not an instruction-following model: it will read the
words, but it will not take direction. **The goal is to make a lightweight TTS model approximate the
behaviour of a much more expensive one** — using signal processing and a
familiar API, so a hobby lab gets narration that feels closer to a hosted model
without paying for it.

Concretely, `instructions` and `[emotion:…]` become measurable prosody (rate,
pitch, gain and EQ), the whole file is mastered to a broadcast-ish loudness
target, and the wire format is OpenAI's. The model stays Kokoro-82M.

This is a hobby project for my self-hosted lab. It runs on a Dell OptiPlex 7060
(Intel Core i5-8500T, 16 GB RAM, Intel UHD 630 — no discrete GPU). The engine is
CPU-only; the gateway is a thin FastAPI + ffmpeg shim. It is built for one
household on a LAN, not for multi-tenant production.

**In scope:** narration, podcast-style dialogue, assistant speech, OpenAI client
compatibility, and a bounded LAN service.

**Out of scope:** training or fine-tuning, voice cloning, parity with OpenAI's
instruction-following model, and real-time mastering while streaming.

## How it works

```
client ──POST /v1/audio/speech──▶ kokoro-tone (FastAPI + ffmpeg, no model)
                                    │  parse markup, map instructions
                                    ▼
                                  kokoro (Kokoro-FastAPI, Kokoro-82M)
                                    │  raw PCM back
                                    ▼
                                  kokoro-tone: prosody → warmth → loudness → encode
```

The gateway is a client of the engine, not a fork. It asks for raw PCM, applies
per-turn prosody, concatenates the turns, masters the whole file once, and
encodes it a single time. The engine is a pinned dependency and is never
patched.

## Quick start

```bash
cp .env.example .env          # set KOKORO_FASTAPI_API_KEY and KOKORO_TONE_API_KEY
make up                       # docker compose up -d --build
```

`make up` reads `engine.version` and passes it to compose as `KOKORO_VERSION`.
The gateway image is built locally; the engine image is pulled. The two services
share a private compose network, so other containers in the same stack reach the
gateway at `http://kokoro-tone:8000`; nothing is published to the host by
default.

To reach it from the host or another stack, add a port mapping (for example
`127.0.0.1:8000:8000`) or attach it to your proxy's network. Read
[Security](#security) first: the gateway is designed to sit behind a proxy on a
trusted LAN.

## What this adds over the engine

Kokoro-FastAPI is the engine, and it is good at what it does: inference,
voices, blending and chunking. The table below is the set of presentation-layer
features I wanted on top of its OpenAI endpoint. The gateway supplies them as a
client, without patching the engine, so the engine can be upgraded
independently.

| Capability | Engine's OpenAI endpoint | kokoro-tone | Proven by |
|---|---|---|---|
| `instructions` | accepted but not applied; the response has no prosody change | matched to a prosody preset (warm, bright, sad, tired, angry, whisper, excited) | `tests/test_prosody.py` |
| Pitch | applies `rate`, but not pitch or volume | formant-preserving shift via `rubberband`, clamped | `tests/test_audio.py` |
| Per-preset tone | not part of the endpoint | rate, pitch, gain and EQ per preset, each measured | `tests/test_audio.py` |
| Mastering | dry model output | low-shelf, high-shelf, compression, tanh saturation, one linear gain to −16 LUFS, −1 dBTP limiter | `tests/test_audio.py` |
| Loudness across turns | not applicable | one gain for the whole file; a whispered turn is never normalised back up | `tests/test_audio.py` |
| Multi-turn `input` | the OpenAI endpoint takes a string only | an array of `{text, voice, instructions}` turns, rendered with gaps into one file | `tests/test_api.py` |
| `[emotion:…]` | not a recognised tag | preset selector, sticky per turn | `tests/test_parse.py` |
| OpenAI SSE | `stream: true` returns chunked audio, not the OpenAI event contract | `stream_format: "sse"` → `speech.audio.delta` frames + `speech.audio.done` | `tests/test_api.py` |
| OpenAI errors | FastAPI `{"detail": …}` | `{"error": {message, type, param, code}}`, 400/401/413/502 mapped | `tests/test_api.py` |
| Alias maps | `voice_aliases` is server-side config, not an OpenAI request field | `voice` accepts a map, with an `id` entry setting the default | `tests/test_api.py` |
| Input bounds | not part of the endpoint | every request is capped (rate, pitch, pause, turns, aliases, body) with env overrides | `tests/test_security.py` |

**Emotion is prosody, not acting.** Kokoro's glottal source is fixed: a filter
can move pitch, rate, loudness and spectral tilt, but it cannot add breathiness,
creak, a smile or F0 *range* inside a sentence. `[emotion:sad]` means "slower,
lower, darker", not "sad". The point is to approximate the *behaviour* of a
larger model, not to claim its range.

## What the engine already does

Not reimplemented here — available from the engine, and outside this gateway's
contract except where noted:

- multi-speaker input via `[voice:…]` and voice aliases (voice strings are
  passed through)
- weighted voice blending (`af_bella(2)+af_sky(1)`) and 72 bundled voices
  (passed through)
- SSML subset and IPA tags (carried in a plain passthrough string)
- sentence chunking, six output formats and word timestamps (engine features;
  the gateway requests PCM and encodes once, and does not expose timestamps)
- OpenAI voice-name mapping (`alloy` → `am_adam`, …) (resolved by the engine)

## Use cases

The feature set is not one product. These are the shapes it is built for, and
which parts each one leans on.

| If you want… | Send | Features that matter | Engine subset |
|---|---|---|---|
| **Narrate a long script or audiobook chapter** | a string with `[voice:]`, `[emotion:]`, `[pause:]`, or a turn array | multi-turn rendering, sticky tags, one mastered file, a loudness target per deliverable | `POST /v1/audio/speech` with `response_format: pcm`, one call per turn |
| **Produce a two-host podcast** | an alias map (`id`, `host_a`, `host_b`) and per-turn `instructions` | alias defaults, voice blends, per-turn prosody, whole-file mastering so both hosts sit at the same loudness | `voice` names and blends, passed through unchanged |
| **Speak an assistant's reply in a chat UI** | a plain string, no tags | passthrough, OpenAI SDK compatibility, `stream: true` or `stream_format: "sse"`, no mastering latency | engine streaming and OpenAI voice aliases |
| **Direct a performance from a transcript** | `instructions: "sad and tired"` or `[emotion:whisper]` | keyword-to-preset mapping, formant-preserving pitch, per-turn rate and gain, relative gain that survives the master | `rate` on the engine call; the gateway owns pitch, gain and EQ |
| **Read text aloud slowly for accessibility** | `[rate:0.8]` and `[pause:800ms]` | clamped rate, bounded pauses, a configurable loudness target, input and body caps | `POST /v1/audio/speech` per segment |
| **Dubbing or dialogue in several languages** | `[voice:ff_siwis]`, `[voice:jm_kumo]`, blends | 72 bundled voices, blends, alias maps, one file with gaps | `voice` strings, passed through |
| **Drive it from an existing OpenAI client** | `model: "kokoro-v1_0"`, `voice: "alloy"` | OpenAI model/voice aliases, OpenAI error envelope, SSE | OpenAI voice-name mapping in the engine |
| **Put a bounded service on a LAN** | any of the above | env-tunable rate/pitch/pause/turn/alias/body caps, bearer token list, no model passthrough | `API_KEYS` bearer, `GET /health` |

### The Kokoro-FastAPI subset this uses

The gateway is a client of the engine, not a fork. Every use case above is
served by this subset:

| Surface | Used for | Used by |
|---|---|---|
| `POST /v1/audio/speech`, `response_format: pcm` | raw PCM in, one encode out | every rendered request |
| `voice` names, blends (`a(2)+b(1)`), OpenAI aliases | passed through unchanged | podcast, dubbing, OpenAI-client cases |
| `API_KEYS` bearer | upstream auth | all cases |
| `GET /health`, `GET /v1/audio/voices` | gateway health, voice list | operations, voice discovery |
| `stream: true` | SSE passthrough | assistant/chat and any passthrough case |

Nothing else in the engine is required. The gateway does not call
`/dev/dialogue` or request word timestamps, and for rendered requests it parses
`[voice:]`, `[emotion:]`, `[pause:]` and `[rate:]` itself rather than relying on
the engine's SSML or IPA handling. Those surfaces remain available if you call
the engine directly, but they are outside this contract.

The trade-off is deliberate: **a request either streams unmastered or is
mastered after rendering.** The assistant case takes the first path; the
narration and podcast cases take the second. See
[Passthrough, rendering and gain](#passthrough-rendering-and-gain).

## Requirements

| Component | Version | Why | Install |
|---|---|---|---|
| **Kokoro-FastAPI** | pinned in `engine.version` | the inference engine this gateway wraps | `make up` (pulls the image) |
| ffmpeg | ≥ 5.1 with librubberband | formant-preserving pitch, encoding | bundled in the image |
| Docker + Compose | v2 | container path | system package |
| Python / uv | 3.12 / 0.13 | development | `mise install` |

Upstream ships weekly. The pin is deliberate: bumping it is a reviewed change,
and the subset above is re-checked before the tag moves. `engine.version` is the
only place the engine tag is written; `make pin-check` fails the build if a
literal tag appears anywhere else.

## Model

This gateway serves exactly one model: **`kokoro-v1_0`** (Kokoro-82M v1.0),
loaded by the pinned engine. `GET /v1/models` reports that id and nothing else.

For wire compatibility the request may name `kokoro`, `tts-1` or `tts-1-hd`;
all three resolve to the same model. `gpt-4o-mini-tts` is rejected: it names
OpenAI's instruction-following TTS model, and this service is not that. The
`instructions` field is honoured here for every request, as a prosody preset —
not as OpenAI's model behaviour.

Voice names are a separate matter: OpenAI's 13 names resolve to Kokoro voices
via the engine's mapping, so `alloy` is a Kokoro voice, not OpenAI's `alloy`.

## Usage

Every example assumes `Authorization: Bearer $KOKORO_TONE_API_KEY` and
`http://kokoro-tone:8000/v1` (the compose service name, reachable from another
container on the same network).

### Passthrough — unmastered, fastest

```bash
curl -sS http://kokoro-tone:8000/v1/audio/speech \
  -H "Authorization: Bearer $KOKORO_TONE_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"model":"kokoro-v1_0","voice":"af_bella","input":"The rain had stopped by morning."}' \
  -o line.mp3
```

No tags and no `instructions` → forwarded whole. Nothing is gained or mastered.

### `instructions` → prosody (rendered, mastered)

```bash
curl -sS http://kokoro-tone:8000/v1/audio/speech \
  -H "Authorization: Bearer $KOKORO_TONE_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"input":"The rain had stopped by morning.","instructions":"sad and tired","response_format":"wav"}' \
  -o line.wav
```

### Markup: three speakers, per-turn emotion, an explicit pause

```json
{
  "input": "[voice:af_heart] [emotion:warm] The rain had stopped by morning.\n[voice:am_michael] [emotion:tired] Did anyone check the gauge?\n[pause:1.2s]\n[voice:bf_emma] I did — it is over the wall.",
  "response_format": "mp3"
}
```

Tags are sticky: `[voice:]` and `[emotion:]` apply until changed. `[pause:]`
adds silence after the turn it follows; `[rate:]` multiplies the current rate.
Unknown `[tag:value]` pairs are left in the text untouched.

### Blend and rate

```json
{
  "input": "[voice:af_bella(2)+af_sky(1)] [rate:0.9] [emotion:bright] Morning, everyone.",
  "response_format": "mp3"
}
```

`[rate:]` multiplies the preset rate and is clamped to the configured range.

### Turn array (OpenRouter shape)

```json
{
  "input": [
    {"text": "Did it land?", "voice": "af_bella", "instructions": "excited"},
    {"text": "It did.", "voice": "am_michael"}
  ],
  "response_format": "mp3"
}
```

### Alias map, with `id` as the default

```json
{
  "voice": {
    "id": "narrator",
    "narrator": "af_heart",
    "hero": "am_michael(2)+am_puck(1)"
  },
  "input": "[voice:narrator] The rain had stopped.\n[voice:hero] Did anyone check the gauge?",
  "response_format": "mp3"
}
```

Alias values may also be `{"id": "af_heart"}`. An `id` entry accepts either
shape.

### OpenAI voice object

```json
{"voice": {"id": "af_bella"}, "input": "Hello.", "response_format": "mp3"}
```

### SSE

```bash
curl -N -sS http://kokoro-tone:8000/v1/audio/speech \
  -H "Authorization: Bearer $KOKORO_TONE_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"input":"[voice:af_bella] Hello.","stream_format":"sse","response_format":"mp3"}'
```

### Errors

```bash
curl -sS http://kokoro-tone:8000/v1/audio/speech \
  -H "Authorization: Bearer $KOKORO_TONE_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"input":"[voice:nope] Hello."}'
# 400 {"error":{"message":"Voice 'nope' not found. …","type":"invalid_request_error","param":null,"code":null}}
```

### OpenAI Python client

```python
import os
from openai import OpenAI

client = OpenAI(
    base_url="http://kokoro-tone:8000/v1",
    api_key=os.environ["KOKORO_TONE_API_KEY"],
)
audio = client.audio.speech.create(
    model="kokoro-v1_0",
    voice="af_heart",
    input="[emotion:warm] The rain had stopped by morning.",
)
audio.write_to_file("line.mp3")
```

### Batch transcript

With the stack running, render a transcript through the gateway container:

```bash
make narrate FILE=script.txt OUT=narration.mp3
```

## Bounds and limits

Every client-facing feature has a bound, and every bound has an environment
variable. The defaults are what a LAN-only service can afford; raise one
deliberately.

| Variable | Default | Bounds |
|---|---|---|
| `MIN_RATE` / `MAX_RATE` | `0.75` / `1.3` | multiplier applied to a turn's rate |
| `MIN_PITCH` / `MAX_PITCH` | `-3.0` / `3.0` | semitones |
| `MIN_SPEED` / `MAX_SPEED` | `0.25` / `4.0` | OpenAI `speed` field |
| `MAX_PAUSE_MS` | `30000` | one `[pause:]` tag |
| `MAX_TOTAL_PAUSE_MS` | `60000` | all `[pause:]` tags in a request |
| `MAX_TURNS` | `100` | turns in an array `input` |
| `MAX_ALIASES` | `64` | entries in a `voice` map |
| `MAX_INSTRUCTIONS_CHARS` | `2000` | `instructions` per request or turn |
| `MAX_INPUT_CHARS` | `4096` | total input characters |
| `MAX_VOICE_CHARS` | `200` | one voice name or blend |
| `MAX_BODY_BYTES` | `1048576` | whole JSON body; larger is `413` |
| `SEGMENT_CHARS` | `1000` | split long turns at sentence boundaries |
| `TURN_GAP_MS` | `350` | silence between turns without an explicit pause |

A min above its max is rejected at startup and the default pair is used. The
loudness target has its own range (`-30…-12` LUFS) because it is a mastering
choice, not a request bound.

## Voices and blends

`voice` (and `[voice:…]`) takes a Kokoro voice name, a blend, or one of
OpenAI's 13 aliases. The gateway passes the string through unchanged — the
engine owns validation and the voice inventory.

```
af_bella                      single voice
af_bella+af_sky               equal weights
af_bella(2)+af_sky(1)         2:1 blend
af_bella(3)-af_sky(1)         subtractive
```

A blend is a weighted sum of the per-voice style tensors, normalised by the
total weight. It moves timbre, not prosody. Weights must be positive; `+`/`-`
cannot be adjacent or lead/trail. Spaces are stripped.

`GET /v1/audio/voices` returns the authoritative list, cached five minutes.
OpenAI names resolve in the engine, not here: `alloy`→`am_adam`, `ash`→
`af_nicole`, `ballad`→`bm_fable`, `coral`→`bf_emma`, `echo`→`af_bella`,
`fable`→`af_sarah`, `marin`→`af_heart`, `nova`→`bf_isabella`, `onyx`→
`bm_george`, `sage`→`am_michael`, `shimmer`→`af_sky`, `cedar`→`am_fenrir`,
`verse`→`am_puck`.

## `instructions` → prosody

Free text is matched against a keyword table. `[emotion:x]` overrides it per
turn. Rate and pitch are clamped to the configured bounds.

| Preset | rate | pitch | gain | EQ |
|---|---|---|---|---|
| warm | ×0.95 | −1 st | +1 dB | low-shelf +1.5 dB @ 250 Hz |
| bright | ×1.05 | +1 st | +1 dB | high-shelf +1.5 dB @ 6 kHz |
| sad | ×0.88 | −2 st | −3 dB | lowpass 6 kHz |
| tired | ×0.90 | −1 st | −2 dB | lowpass 8 kHz |
| angry | ×1.15 | +1 st | +4 dB | treble +2 dB, compression 3:1 |
| whisper | ×0.85 | 0 | −8 dB | highpass 300 Hz, lowpass 8 kHz |
| excited | ×1.12 | +2 st | +2 dB | treble +3 dB |

Pitch uses `rubberband` with formant preservation. If ffmpeg lacks it, the chain
falls back to `asetrate` + `atempo`, which shifts formants and is audible.

## Mastering

Applied once, to the whole file:

```
low-shelf  +2.0 dB @ 250 Hz
high-shelf −1.5 dB @ 6 kHz
acompressor ratio 2.5, attack 15 ms, release 200 ms
tanh saturation at low drive
single linear gain → −16 LUFS (podcast default; `LOUDNESS_LUFS`)
alimiter −1 dBTP
```

Loudness is measured *after* the warmth chain and corrected with one linear
gain. It is never normalised per segment: that would boost a whispered line
back to normal and erase the performance.

## Passthrough, rendering and gain

Three paths, and they do not gain the same way:

| Request | Gain |
|---|---|
| **Passthrough + `stream: true`** (plain string, no tags, no `instructions`) | none — the engine's bytes are piped through unmastered |
| **Passthrough + `stream_format: "sse"`** | none — the engine's chunks are wrapped as deltas |
| **Rendered + `stream_format: "sse"`** (tags or `instructions`) | full chain, buffered; the first delta arrives after mastering |
| **Rendered + `stream: true`** | `stream` is ignored; the body is a complete file |

Preset gain is relative (whisper is −8 dB before the master), so it survives the
master's single absolute gain. The limiter is last, so a hot preset cannot push
past −1 dBTP.

The honest limit: **mastering and incremental streaming cannot both happen.**
Real-time gain would mean measuring per chunk, which is the per-segment
normalisation the design forbids. If a client needs mastering, it gets the file
after rendering; if it needs bytes as they are produced, it gets the engine's
unmastered output.

## Streaming

`stream_format` follows the OpenAI spec:

- `"audio"` (default) — a binary body in `response_format`.
- `"sse"` — `text/event-stream`, each chunk a base64 `speech.audio.delta`, then
  a final `speech.audio.done` carrying estimated usage.

Token counts are estimated at four characters per token — Kokoro reports no
usage of its own.

## Errors

Errors use OpenAI's envelope, so SDK error handling behaves:

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": "input", "code": "invalid_value"}}
```

`401` is `authentication_error` / `invalid_api_key`, validation failures are
`400` (not `422`), oversized bodies are `413`, and upstream failures are `502` /
`server_error`.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `KOKORO_FASTAPI_API_KEY` | — | bearer the gateway sends to the engine; becomes the engine's `API_KEYS` |
| `KOKORO_TONE_API_KEY` | — | bearer required by the gateway; empty disables auth |
| `KOKORO_TONE_API_KEYS` | — | comma-separated tokens, merged with the singular key |
| `UPSTREAM_BASE_URL` | `http://kokoro:8880/v1` | engine seam |
| `UPSTREAM_API_KEY` | — | explicit engine key; empty reuses `KOKORO_FASTAPI_API_KEY` |
| `UPSTREAM_MODEL` | `kokoro` | engine mapping key; `hexgrad/kokoro-82m` for OpenRouter |
| `DEFAULT_VOICE` | `af_heart` | voice when the request omits one |
| `LOUDNESS_LUFS` | `-16` | podcast target; clamped to −30…−12 |

The `MIN_*`/`MAX_*` request bounds are in [Bounds and limits](#bounds-and-limits).

### Token rotation

1. Append the new token: `KOKORO_TONE_API_KEYS=old,new`, restart the gateway.
2. Move clients to the new token.
3. Drop the old one, restart again.

Engine side: add the new key to `KOKORO_FASTAPI_API_KEY`, restart the gateway,
then remove the old key from the engine and restart it.

### OpenRouter fallback

Documented, not wired in. Point `UPSTREAM_BASE_URL` at
`https://openrouter.ai/api/v1`, set `UPSTREAM_API_KEY` to the OpenRouter key and
`UPSTREAM_MODEL` to `hexgrad/kokoro-82m`. Multi-speaker input, blending and
aliases are lost; single-voice requests still work.

## Development

mise pins the toolchain; make runs the tasks; uv manages dependencies.

```bash
mise install      # python + uv
uv sync           # dependencies from uv.lock
make check        # format, lint, security and pin checks
make test         # pytest
```

| Target | Does |
|---|---|
| `make install` | `uv sync --frozen` |
| `make lint` / `make fmt` | ruff check / ruff format |
| `make check` | format check, lint, `security-check`, `pin-check` |
| `make security-check` | refuse dynamic-evaluation sinks in `src/` |
| `make pin-check` | the engine tag appears only in `engine.version` |
| `make test` | pytest |
| `make version` | print the CalVer version |
| `make engine` | print the pinned engine tag |
| `make build` / `make up` / `make down` / `make logs` | container lifecycle |
| `make narrate FILE=script.txt OUT=out.mp3` | render a transcript |
| `make analyze FILE=out.mp3` | measure F0, syllables/s, centroid, LUFS, TP |

`ffmpeg` must be on `PATH` for the audio tests.

## Versioning

CalVer in the form `YYYY.MM.MICRO`. `MICRO` starts at `0` each month and
increments per release; tags are prefixed with `v`. The version lives in
`src/kokoro_tone/__init__.py`, and CI's `tag-check` fails if a tag disagrees
with it.

## Security

Input is treated as data. The tag grammar is a closed allowlist, voices and
aliases are validated against anchored patterns, control characters are
stripped, the model name is never forwarded, and ffmpeg filter expressions are
built only from constants and formatted floats. `make security-check` fails the
build if a dynamic-evaluation sink appears in `src/`.

Report vulnerabilities privately — see [SECURITY.md](SECURITY.md).

## Credits

This project would not exist without the work it builds on:

- **[Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI)** by remsky — the
  inference engine. Apache-2.0. It is pulled as a pinned container image and is
  not modified or redistributed here.
- **[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)** by hexgrad — the
  model. Apache-2.0. The weights are baked into the engine image.
- **[StyleTTS 2](https://github.com/yl4579/StyleTTS2)** and ISTFTNet — the
  architectures Kokoro is built on.
- **ffmpeg**, **librubberband** and **libmp3lame/libopus** — the audio chain.
- **OpenAI's speech API** — the wire shape this gateway imitates. OpenAI is a
  trademark of OpenAI; this project is not affiliated with or endorsed by them.

The gateway code in this repository is MIT-licensed. The engine and model keep
their own Apache-2.0 licences; nothing from them is vendored here.

### If you are one of the upstream maintainers

Thank you. This is an independent wrapper, not a fork, and it does not patch the
engine. If something here is inaccurate about the engine's behaviour, or if you
would prefer different attribution, please open an issue and I will fix it.

## License

MIT. See [LICENSE](LICENSE).
