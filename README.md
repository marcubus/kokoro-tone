# kokoro-tone

[![ci](https://github.com/marcubus/kokoro-tone/actions/workflows/ci.yml/badge.svg)](https://github.com/marcubus/kokoro-tone/actions/workflows/ci.yml)
[![license: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![python](https://img.shields.io/badge/python-3.12-blue.svg)](pyproject.toml)

An OpenAI-compatible speech endpoint for a small, local TTS model - built on
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
behaviour of a much more expensive one** - using signal processing and a
familiar API, so a hobby lab gets narration that feels closer to a hosted model
without paying for it.

Concretely, `instructions` and `[emotion:…]` become measurable prosody (rate,
pitch, gain and EQ), the whole file is mastered to a broadcast-ish loudness
target, and the wire format is OpenAI's. The model stays Kokoro-82M.

This is a hobby project for my self-hosted lab. It runs on a Dell OptiPlex 7060
(Intel Core i5-8500T, 16 GB RAM, Intel UHD 630 - no discrete GPU). The engine is
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
                                  kokoro-tone: prosody → tone chain → loudness → encode
```

The gateway is a client of the engine, not a fork. It asks for raw PCM, applies
per-turn prosody, concatenates the turns, masters the whole file once, and
encodes it a single time. The engine is a pinned dependency and is never
patched.

## Quick start

Two ways to run the gateway: pull the released image, or build it locally.

### Released image

```bash
cp .env.example .env          # set KOKORO_FASTAPI_API_KEY and KOKORO_TONE_API_KEY
make up-image                 # pull ghcr.io/marcubus/kokoro-tone and start both services
```

`KOKORO_TONE_VERSION` selects the image tag (default `latest`); pin an exact
release such as `2026.11.2` for reproducible deployments. Images are published
for `linux/amd64` and `linux/arm64`, signed with cosign (keyless) and scanned
with Trivy on release.

### Local build

```bash
cp .env.example .env
make up                       # docker compose up -d --build
```

`make up` reads `engine.version` and passes it to compose as `KOKORO_VERSION`.
The engine image is always pulled. The two services share a private compose
network, so other containers in the same stack reach the gateway at
`http://kokoro-tone:8000`; nothing is published to the host by default.

### Verifying a release image

```bash
cosign verify ghcr.io/marcubus/kokoro-tone:2026.11.2 \
  --certificate-identity-regexp '^https://github.com/marcubus/kokoro-tone/.github/workflows/release.yml@refs/tags/' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

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
| `instructions` | accepted but not applied; the response has no prosody change | keyword-matched to one preset (warm, bright, sad, tired, angry, whisper, excited); the file-wide default that `[emotion:]` overrides per turn | `tests/test_prosody.py` |
| Pitch | applies `rate`, but not pitch or volume | formant-preserving shift via `rubberband`, clamped | `tests/test_audio.py` |
| Per-preset tone | not part of the endpoint | rate, pitch, gain and EQ per preset, each measured | `tests/test_audio.py` |
| Mastering | dry model output | low-shelf, high-shelf, compression, tanh saturation, one linear gain to −16 LUFS, limiter at −1 dBFS sample peak | `tests/test_audio.py` |
| Loudness across turns | not applicable | one gain for the whole file; a whispered turn is never normalised back up | `tests/test_audio.py` |
| Voice loudness | dry model output, 9.2 dB spread across English voices | static per-voice casting gain for all 72 measured voices, blends interpolated, on both the mastered and passthrough paths | `tests/test_levelling.py` |
| Multi-turn `input` | the OpenAI endpoint takes a string only | an array of `{text, voice, instructions}` turns, rendered with gaps into one file | `tests/test_api.py` |
| `[emotion:…]` | not a recognised tag | preset selector, sticky across turns and voices; `[emotion:neutral]` clears it | `tests/test_parse.py` |
| OpenAI SSE | `stream: true` returns chunked audio, not the OpenAI event contract | `stream_format: "sse"` → `speech.audio.delta` frames + `speech.audio.done` | `tests/test_api.py` |
| OpenAI errors | FastAPI `{"detail": …}` | `{"error": {message, type, param, code}}`, 400/401/413/502 mapped | `tests/test_api.py` |
| Alias maps | `voice_aliases` is server-side config, not an OpenAI request field | `voice` accepts a map, with an `id` entry setting the default | `tests/test_api.py` |
| Input bounds | not part of the endpoint | every request is capped (rate, pitch, pause, turns, aliases, body) with env overrides | `tests/test_security.py` |
| Word → sound mapping | none | ordered literal rules with optional context, per-voice sets and server aliases | `tests/test_pronounce.py` |
| Number and acronym normalisation | none | Australian cardinals, decimals, percent, ranges and letter-spelled acronyms, all opt-in | `tests/test_normalise.py` |

**Emotion is prosody, not acting.** Kokoro's glottal source is fixed: a filter
can move pitch, rate, loudness and spectral tilt, but it cannot add breathiness,
creak, a smile or F0 *range* inside a sentence. `[emotion:sad]` means "slower,
lower, darker", not "sad". The point is to approximate the *behaviour* of a
larger model, not to claim its range.

## What the engine already does

Not reimplemented here - available from the engine, and outside this gateway's
contract except where noted:

- multi-speaker input via `[voice:…]` and voice aliases (voice strings are
  passed through)
- weighted voice blending (`af_bella(2)+af_sky(1)`) and 72 bundled voices
  (passed through)
- SSML subset and IPA tags (carried in a plain passthrough string)
- sentence chunking, six output formats and word timestamps (engine features;
  the gateway requests PCM and encodes once, and does not expose timestamps)
- OpenAI voice-name mapping (`alloy` → `am_adam`, …) (resolved by the engine)

## Model

This gateway serves exactly one model: **`kokoro-v1_0`** (Kokoro-82M v1.0),
loaded by the pinned engine. `GET /v1/models` reports that id and nothing else.

For wire compatibility the request may name `kokoro`, `tts-1` or `tts-1-hd`;
all three resolve to the same model. `gpt-4o-mini-tts` is rejected: it names
OpenAI's instruction-following TTS model, and this service is not that. The
`instructions` field is honoured here for every request, as a prosody preset -
not as OpenAI's model behaviour.

Voice names are a separate matter: OpenAI's 13 names resolve to Kokoro voices
via the engine's mapping, so `alloy` is a Kokoro voice, not OpenAI's `alloy`.

## Requirements

| Component | Version | Why | Install |
|---|---|---|---|
| **Kokoro-FastAPI** | pinned in `engine.version` | the inference engine this gateway wraps | `make up` (pulls the image) |
| ffmpeg | ≥ 5.1 with librubberband | formant-preserving pitch, encoding | built into the image ([details](docs/development.md#the-pinned-ffmpeg-build)) |
| Docker + Compose | v2 | container path | system package |
| Python / uv | 3.12 / 0.13 | development | `mise install` |

Upstream ships weekly. The pin is deliberate: bumping it is a reviewed change,
and the subset above is re-checked before the tag moves. `engine.version` is the
only place the engine tag is written; `make pin-check` fails the build if a
literal tag appears anywhere else.

See [the pinned ffmpeg build](docs/development.md#the-pinned-ffmpeg-build) for
why the image compiles its own binary and what a new filter requires.

## Usage

Every example assumes `Authorization: Bearer $KOKORO_TONE_API_KEY` and
`http://kokoro-tone:8000/v1` (the compose service name, reachable from another
container on the same network).

### Passthrough - unmastered, fastest

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
  "input": "[voice:af_heart] [emotion:warm] The rain had stopped by morning.\n[voice:am_michael] [emotion:tired] Did anyone check the gauge?\n[pause:1.2s]\n[voice:bf_emma] I did - it is over the wall.",
  "response_format": "mp3"
}
```

Tags are sticky: `[voice:]` and `[emotion:]` apply until changed. `[pause:]`
adds silence after the turn it follows; `[rate:]` multiplies the current rate.
Unknown `[tag:value]` pairs are left in the text untouched. `[emotion:neutral]`
clears the emotion, so a script can return to the request default mid-file.

Changing `[voice:]` resets neither the emotion nor the rate, so a tag set once
follows every later speaker until it is changed. Request `instructions` set a
file-wide tone that `[emotion:]` overrides per turn; `[emotion:neutral]` returns
to it, which is how a file keeps a baseline and still punctuates it.

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

More request shapes, voices, streaming and errors are in
[`docs/usage.md`](docs/usage.md). Pronunciation and normalisation are in
[`docs/pronunciation.md`](docs/pronunciation.md); the prosody presets and the
mastering chain are in [`docs/audio.md`](docs/audio.md).

## Documentation

The README is the overview. The reference material lives in `docs/`:

| Document | Covers |
|---|---|
| [`docs/usage.md`](docs/usage.md) | request examples, use cases, the engine subset, voices and blends, streaming, errors |
| [`docs/pronunciation.md`](docs/pronunciation.md) | `pronunciations`, heteronym senses and guessing, `normalise`, per-voice sets, the deployment lexicon |
| [`docs/audio.md`](docs/audio.md) | `instructions` and `[emotion:]` presets, mastering, passthrough vs rendered gain |
| [`docs/voice-loudness.md`](docs/voice-loudness.md) | the per-voice loudness review behind the mastering and casting decisions |
| [`docs/configuration.md`](docs/configuration.md) | every environment variable and bound, logs, token rotation, the OpenRouter fallback |
| [`docs/development.md`](docs/development.md) | toolchain, `make` targets, the pinned ffmpeg build, versioning |
| [`docs/adr/README.md`](docs/adr/README.md) | architecture decision records and why they exist |
| [`lexicon/README.md`](lexicon/README.md) | the deployment lexicon's JSON schema |

## Security

Input is treated as data. The tag grammar is a closed allowlist, voices and
aliases are validated against anchored patterns, control characters are
stripped, the model name is never forwarded, and ffmpeg filter expressions are
built only from constants and formatted floats. Pronunciation rules are literal
strings, never regular expressions, brackets are rejected so a replacement
cannot inject markup, and the deployment file's aliases use the same voice
grammar as `[voice:]` tags. `make security-check` fails the build if a
dynamic-evaluation sink appears in `src/`.

Report vulnerabilities privately - see [SECURITY.md](SECURITY.md).

## Credits

This project would not exist without the work it builds on:

- **[Kokoro-FastAPI](https://github.com/remsky/Kokoro-FastAPI)** by remsky - the
  inference engine. Apache-2.0. It is pulled as a pinned container image and is
  not modified or redistributed here.
- **[Kokoro-82M](https://huggingface.co/hexgrad/Kokoro-82M)** by hexgrad - the
  model. Apache-2.0. The weights are baked into the engine image.
- **[StyleTTS 2](https://github.com/yl4579/StyleTTS2)** and ISTFTNet - the
  architectures Kokoro is built on.
- **ffmpeg**, **librubberband** and **libmp3lame/libopus** - the audio chain.
- **OpenAI's speech API** - the wire shape this gateway imitates. OpenAI is a
  trademark of OpenAI; this project is not affiliated with or endorsed by them.

The gateway code in this repository is MIT-licensed. The engine and model keep
their own Apache-2.0 licences; nothing from them is vendored here.

### If you are one of the upstream maintainers

Thank you. This is an independent wrapper, not a fork, and it does not patch the
engine. If something here is inaccurate about the engine's behaviour, or if you
would prefer different attribution, please open an issue and I will fix it.

## License

MIT. See [LICENSE](LICENSE).
