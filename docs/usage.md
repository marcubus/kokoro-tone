# Usage

Request shapes, the engine subset they rely on, and the wire details.

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
| **Turn a researched script into an episode** | plain prose plus `normalise: true` and a `pronunciations` map | digits to words, acronyms spelled out, homophones and heteronyms mapped, per-voice rules | plain `/v1/audio/speech` |
| **Catch a word the script left ambiguous** | nothing to send | a guess, rendered, with `X-Kokoro-Tone-Guessed` naming each assumption; `ambiguity: "error"` for a hard stop | plain `/v1/audio/speech` |
| **Put a bounded service on a LAN** | any of the above | env-tunable rate/pitch/pause/turn/alias/body caps, bearer token list, no model passthrough | `API_KEYS` bearer, `GET /health`, OTLP metrics |

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
[Passthrough, rendering and gain](audio.md#passthrough-rendering-and-gain).

## Request examples

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
Unknown `[tag:value]` pairs are left in the text untouched.

`[emotion:neutral]` clears the emotion rather than selecting a preset, so a
script can return to the request default mid-file:

```json
{
  "input": "[emotion:warm] The rain had stopped by morning.\n[emotion:neutral] I did - it is over the wall.",
  "response_format": "mp3"
}
```

Clearing removes the turn-level override, not the request-level one: if the
request also carried `instructions`, those apply to the cleared lines as usual.
With no `instructions`, they are neutral.

### `instructions` set the file-wide tone

Request-level `instructions` choose one preset for every turn that does not name
its own, and `[emotion:neutral]` returns to it. That is how a file keeps a
baseline tone and still punctuates it:

```json
{
  "instructions": "warm",
  "input": "The rain had stopped by morning.\n[emotion:excited] \"It's cleared!\" she said.\n[emotion:neutral] He looked at the sky again.",
  "response_format": "mp3"
}
```

Line 1 is `warm` from `instructions`, line 2 is `excited`, line 3 is `warm`
again - `neutral` cleared the override and exposed the request default.

Within a single turn there is no blending: `[emotion:excited]` alongside
`instructions: "warm"` gives `excited`, not a mixture. The tag replaces the
request-level choice; it does not combine with it.

### Voice, emotion and rate are independent

Changing `[voice:]` resets neither the emotion nor the rate:

```json
{
  "input": "[voice:bm_lewis] [emotion:warm] [rate:0.95] The rain had stopped by morning.\n[voice:af_heart] \"Not for long,\" she said.\n[voice:bm_lewis] He looked at the sky again.",
  "response_format": "mp3"
}
```

All three lines are warm and at ×0.95, including the quote. If the new speaker
should not inherit it, say so - `[voice:af_heart] [emotion:neutral]`. Rate has
no reset tag because it is a multiplier on the preset rate: `[rate:1.0]` means
"no change".

This stickiness belongs to the markup form. In the turn array each turn stands
alone, so an omitted `emotion` or `rate` is simply unset for that turn.

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

## Voices and blends

`voice` (and `[voice:…]`) takes a Kokoro voice name, a blend, or one of
OpenAI's 13 aliases. The gateway passes the string through unchanged - the
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

Every voice is levelled to a common pre-master loudness before mastering; a blend
interpolates its members' gains by weight. The table and method are in
[`voice-loudness.md`](voice-loudness.md).

`GET /v1/audio/voices` returns the authoritative list, cached five minutes.
OpenAI names resolve in the engine, not here: `alloy`→`am_adam`, `ash`→
`af_nicole`, `ballad`→`bm_fable`, `coral`→`bf_emma`, `echo`→`af_bella`,
`fable`→`af_sarah`, `marin`→`af_heart`, `nova`→`bf_isabella`, `onyx`→
`bm_george`, `sage`→`am_michael`, `shimmer`→`af_sky`, `cedar`→`am_fenrir`,
`verse`→`am_puck`.

## Streaming

`stream_format` follows the OpenAI spec:

- `"audio"` (default) - a binary body in `response_format`.
- `"sse"` - `text/event-stream`, each chunk a base64 `speech.audio.delta`, then
  a final `speech.audio.done` carrying estimated usage.

Token counts are estimated at four characters per token - Kokoro reports no
usage of its own.

## Errors

Errors use OpenAI's envelope, so SDK error handling behaves:

```json
{"error": {"message": "...", "type": "invalid_request_error", "param": "input", "code": "invalid_value"}}
```

`401` is `authentication_error` / `invalid_api_key`, validation failures are
`400` (not `422`), oversized bodies are `413`, and upstream failures are `502` /
`server_error`.

