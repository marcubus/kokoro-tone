# Configuration and operations

Every bound, environment variable and log field, plus token rotation and the
OpenRouter fallback.

## Bounds and limits

Every client-facing feature has a bound. The render bounds below are
environment variables; the pronunciation bounds are fixed constants, because a
deployment has no reason to tune them and a bad value only makes the scanner
slower or a request stricter.

| Variable | Default | Bounds |
|---|---|---|
| `MIN_RATE` / `MAX_RATE` | `0.75` / `1.3` | multiplier applied to a turn's rate |
| `MIN_PITCH` / `MAX_PITCH` | `-3.0` / `3.0` | semitones |
| `MIN_SPEED` / `MAX_SPEED` | `0.25` / `4.0` | OpenAI `speed` field |
| `MAX_PAUSE_MS` | `30000` | one `[pause:]` tag |
| `MAX_TOTAL_PAUSE_MS` | `60000` | all `[pause:]` tags in a request |
| `MAX_TURNS` | `100` | turns in an array `input` |
| `MAX_ALIASES` | `64` | entries in the merged alias map (request + server) |
| `MAX_INSTRUCTIONS_CHARS` | `2000` | `instructions` per request or turn |
| `MAX_INPUT_CHARS` | `4096` | total input characters |
| `MAX_VOICE_CHARS` | `200` | one voice name or blend |
| `MAX_BODY_BYTES` | `1048576` | whole JSON body; larger is `413` |
| `SEGMENT_CHARS` | `1000` | split long turns at sentence boundaries |
| `TURN_GAP_MS` | `350` | silence between turns without an explicit pause |

Fixed pronunciation bounds: 256 request rules, 200 characters per rule field,
16 context words, 2048 lexicon rules, 256 KB lexicon file, 64 words per number,
32 voice sets. A `before` cue looks back 2 words.

A min above its max is rejected at startup and the default pair is used. The
loudness target has its own range (`-30…-12` LUFS) because it is a mastering
choice, not a request bound.

## Logs

One line per event on stderr, which is the container's log stream and therefore
Loki. The shape is `event=<name> key=value …`, so `grep event=speech` and Loki's
`| logfmt` both work without a parser.

```
event=lexicon rules=462 voices=0 aliases=0 locale=en-AU overlay=no problems=0
event=speech status=200 passthrough=yes stream=audio chars=16 turns=1 voice=bm_lewis fmt=mp3 normalise=lexicon+acronyms+numbers ms=1132
event=speech status=200 passthrough=no stream=audio chars=26 turns=1 voice=bm_lewis fmt=mp3 normalise=lexicon+acronyms+numbers guess=bass=music upstream_ms=1424 render_ms=79 bytes=60044
event=error path=/v1/audio/speech status=400 detail="ambiguity must be 'guess' or 'ignore'"
```

| Field | Meaning |
|---|---|
| `passthrough` | `yes` forwarded whole, `no` rendered and mastered |
| `chars`, `turns` | the sanitised request, after splitting |
| `normalise` | the layers that ran, `none` if the request turned them off |
| `guess` | the same `word=sense` list the response header carries |
| `upstream_ms` | time in the engine; the rest is `render_ms` (ffmpeg + master) |
| `bytes` | the encoded response |

**The text itself is never logged.** A field is a count, a name, a duration or a
status - the script belongs to the caller, not the operator. `event=lexicon`
appears at startup and on every hot reload, so a bad overlay is visible without
sending a request.

The engine logs separately and defaults to `API_LOG_LEVEL=DEBUG`, which prints
every text chunk verbatim. `compose.yml` sets `INFO`; the chunk text is the
script, so leave it there.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `KOKORO_FASTAPI_API_KEY` | - | bearer the gateway sends to the engine; becomes the engine's `API_KEYS` |
| `KOKORO_TONE_API_KEY` | - | bearer required by the gateway; empty disables auth |
| `KOKORO_TONE_API_KEYS` | - | comma-separated tokens, merged with the singular key |
| `UPSTREAM_BASE_URL` | `http://kokoro:8880/v1` | engine seam |
| `UPSTREAM_API_KEY` | - | explicit engine key; empty reuses `KOKORO_FASTAPI_API_KEY` |
| `UPSTREAM_MODEL` | `kokoro` | engine mapping key; `hexgrad/kokoro-82m` for OpenRouter |
| `DEFAULT_VOICE` | `af_heart` | voice when the request omits one |
| `LOUDNESS_LUFS` | `-16` | podcast target; clamped to −30…−12 |
| `PRONUNCIATION_LEXICON` | `/etc/kokoro-tone/lexicon.json` | the shipped rules, per-voice sets and aliases |
| `PRONUNCIATION_OVERRIDES` | `/etc/kokoro-tone/custom.json` | optional user overlay, composed in memory over the defaults |
| `LOCALE` | `en-AU` | `en-AU`, `en-GB` or `en-US` |
| `NORMALISE_DEFAULT` | `false` | what an absent `normalise` field means |

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

