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
| `MAX_ALIASES` | `64` | aliases a request may add; server aliases are deployment-controlled and not counted |
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
event=error path=/v1/audio/speech status=400 detail="ambiguity must be 'guess', 'ignore' or 'error'"
event=pronunciation_learn chars=5 sense= rules=12
event=pronunciation_forget chars=5 sense= removed=1
event=auth result=forbidden path=/v1/audio/voices slug=listVoices role=default
```

| Field | Meaning |
|---|---|
| `passthrough` | `yes` forwarded whole, `no` rendered and mastered |
| `chars`, `turns` | the sanitised request, after splitting |
| `normalise` | the layers that ran, `none` if the request turned them off |
| `guess` | the same `word=sense` list the response header carries |
| `upstream_ms` | time in the engine; the rest is `render_ms` (ffmpeg + master) |
| `bytes` | the encoded response |
| `chars`, `sense`, `rules`, `removed` | a lexicon write: the word's length, its sense label, the resulting rule count and how many rules `forget` removed |
| `result`, `slug`, `role` | an authentication or authorisation failure: `unauthenticated`/`forbidden`, the operation id, and the role that was recognised. The token is never logged, not even a prefix |

**The text itself is never logged.** A field is a count, a name, a duration or a
status - the script belongs to the caller, not the operator. That includes the
lexicon write events: they carry the word's length, not the word. `event=lexicon`
appears at startup and on every hot reload, so a bad overlay is visible without
sending a request.

The engine logs separately and defaults to `API_LOG_LEVEL=DEBUG`, which prints
every text chunk verbatim. `compose.yml` sets `INFO`; the chunk text is the
script, so leave it there.

## Metrics

The gateway instruments with OpenTelemetry and pushes OTLP/HTTP to a collector.
Metrics are disabled by default: nothing is exported until
`OTEL_EXPORTER_OTLP_ENDPOINT` is set. The gateway has no `/metrics` route of its
own; the collector exposes the Prometheus endpoint. See
[Observability](observability.md) for the instrument list, cardinality rules,
collector configuration and alert examples.

## Configuration

| Variable | Default | Purpose |
|---|---|---|
| `KOKORO_FASTAPI_API_KEY` | - | bearer the gateway sends to the engine; becomes the engine's `API_KEYS` |
| `KOKORO_TONE_API_KEY` | - | `default` bearer; empty (with the admin lists) disables auth |
| `KOKORO_TONE_API_KEYS` | - | comma-separated `default` tokens, merged with the singular key |
| `KOKORO_TONE_ADMIN_KEY` | - | `admin` bearer; may call every operation |
| `KOKORO_TONE_ADMIN_KEYS` | - | comma-separated `admin` tokens, merged with the singular key |
| `KOKORO_TONE_RBAC` | `/etc/kokoro-tone/rbac.toml` | TOML allowlist for the `default` role; a missing file uses the built-in policy |
| `UPSTREAM_BASE_URL` | `http://kokoro:8880/v1` | engine seam |
| `UPSTREAM_API_KEY` | - | explicit engine key; empty reuses `KOKORO_FASTAPI_API_KEY` |
| `UPSTREAM_MODEL` | `kokoro` | engine mapping key; `hexgrad/kokoro-82m` for OpenRouter |
| `LOG_LEVEL` | `INFO` | gateway log level: `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL` |
| `OTEL_EXPORTER_OTLP_ENDPOINT` | empty | OTLP/HTTP collector base URL; empty disables metrics export |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | empty | signal-specific OTLP metrics URL; wins over the base |
| `OTEL_SERVICE_NAME` | `kokoro-tone` | `service.name` resource attribute |
| `OTEL_RESOURCE_ATTRIBUTES` | - | extra OTel resource attributes |
| `OTEL_METRIC_EXPORT_INTERVAL` | `15000` | export interval in milliseconds |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `5000` | export timeout in milliseconds |
| `OTEL_SDK_DISABLED` | `false` | `true` starts no exporter |
| `DEFAULT_VOICE` | `af_heart` | voice when the request omits one |
| `LOUDNESS_LUFS` | `-16` | podcast target; clamped to −30…−12 |
| `VOICE_LEVELLING` | `true` | static per-voice casting gain; `false` leaves the engine's raw level |
| `PRONUNCIATION_LEXICON` | `/etc/kokoro-tone/lexicon.json` | the shipped rules, per-voice sets and aliases |
| `PRONUNCIATION_OVERRIDES` | `/etc/kokoro-tone/custom.json` | optional user overlay, composed in memory over the defaults |
| `PRONUNCIATION_LEARNED` | `/etc/kokoro-tone/learned/learned.json` | machine-written overlay; the only lexicon file the gateway writes |
| `PRONUNCIATION_WRITE` | `false` | enable `POST /v1/pronunciations/learn` and `/forget` |
| `LOCALE` | `en-AU` | `en-AU`, `en-GB` or `en-US` |
| `NORMALISE_DEFAULT` | `false` | what an absent `normalise` field means |
| `KOKORO_TONE_FILTER_MANIFESTS` | unset | a JSON file or directory of `[filter:]` block manifests; see [`plugins.md`](plugins.md) |

The `MIN_*`/`MAX_*` request bounds are in [Bounds and limits](#bounds-and-limits).

### Roles and the allowlist

There are two roles and no more. A token in `KOKORO_TONE_API_KEY(S)` is
`default`; a token in `KOKORO_TONE_ADMIN_KEY(S)` is `admin`. The admin list is
checked first, so a token present in both is an admin.

Authentication and authorisation are separate steps. Every route carries an
OpenAPI `operationId`, and that id - not the path - is the permission the policy
grants:

| Operation id | Route | Default key |
|---|---|---|
| `createSpeech` | `POST /v1/audio/speech` | yes |
| `listModels` | `GET /v1/models` | yes |
| `listVoices` | `GET /v1/audio/voices` | yes |
| `listPronunciations` | `POST /v1/pronunciations/lookup` | yes |
| `createPronunciation` | `POST /v1/pronunciations/learn` | yes |
| `deletePronunciation` | `POST /v1/pronunciations/forget` | yes |
| `compileScript` | `POST /v1/script/compile` | yes |
| `getHealth` | `GET /health` | public |

`getHealth`, `openapi`, `swagger_ui_html` and `redoc_html` are public by
contract: the container healthcheck and the documentation must work without a
credential. That set lives in code, not in the policy file.

A default key may call only the ids named in `[default].allow`; an admin key may
call every id, including one no list names. A route added in a later release is
therefore admin-only for a default key until the file names it. The shipped
example is generated from the built-in defaults and mounted at
`/etc/kokoro-tone/rbac.example.toml`:

```toml
version = 1

[default]
allow = [
  "compileScript",
  "createPronunciation",
  "createSpeech",
  "deletePronunciation",
  "getHealth",
  "listModels",
  "listPronunciations",
  "listVoices",
]
```

Copy it to `rbac.toml` to opt into a narrower policy. **With no file, the
built-in policy grants every operation that exists today**, so an upgrade that
adds keys but no file changes nothing. With no keys at all the policy is inert
and every route is open, exactly as before; the gateway logs one warning if a
file exists in that state.

An unknown `version`, an unknown table or key, or an unknown operation id stops
the process at startup with the file and the offending value named. The policy
file and the code version must be deployed together; rolling the configuration
forward is not supported.

Errors keep the OpenAI envelope:

- Every authentication failure is the same `401 authentication_error` /
  `invalid_api_key` with `WWW-Authenticate: Bearer`, whether the token was
  absent, malformed, unknown or from another deployment. The reason is logged
  with the path and slug, never the token.
- A recognised key on an id its role may not call is `403 permission_error` /
  `insufficient_scope` with `WWW-Authenticate: Bearer error="insufficient_scope"`.
  Authentication is checked first, so a default key on a denied slug is 403 and
  never 401.

`PRONUNCIATION_WRITE_KEY` and the `X-Pronunciation-Key` header are retired:
writes are granted by operation id like every other route. `PRONUNCIATION_WRITE`
still decides whether the write routes may write at all.

### Token rotation

1. Append the new token: `KOKORO_TONE_API_KEYS=old,new`, restart the gateway.
2. Move clients to the new token.
3. Drop the old one, restart again.

Rotate an admin key the same way with `KOKORO_TONE_ADMIN_KEYS=old,new`. An admin
key can rewrite what every listener hears, so treat it as a different class of
credential from a render key.

Engine side: add the new key to `KOKORO_FASTAPI_API_KEY`, restart the gateway,
then remove the old key from the engine and restart it.

### OpenRouter fallback

Documented, not wired in. Point `UPSTREAM_BASE_URL` at
`https://openrouter.ai/api/v1`, set `UPSTREAM_API_KEY` to the OpenRouter key and
`UPSTREAM_MODEL` to `hexgrad/kokoro-82m`. Multi-speaker input, blending and
aliases are lost; single-voice requests still work.

