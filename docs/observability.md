# Observability

The gateway is stateless, so its useful signals are work per request, engine
latency, ffmpeg cost and mastering drift. It instruments with the OpenTelemetry
API and pushes OTLP/HTTP to a collector. The collector owns the Prometheus
endpoint; the gateway itself has no `/metrics` route.

```
kokoro-tone ──OTLP/HTTP :4318──▶ otel-collector ──prometheus :8889──▶ Prometheus ──▶ Grafana
      │                                │
      └── cAdvisor ────────────────────┘   container CPU/mem/restarts
      └── logfmt ──▶ Loki                 event=speech
```

Container metrics stay with cAdvisor. Logs stay as logfmt in Loki. Metrics are
aggregate operator signals, never a copy of the script.

## Configuration

Metrics are **disabled by default**. Nothing is exported until
`OTEL_EXPORTER_OTLP_ENDPOINT` (or the signal-specific endpoint) is set.

| Variable | Default | Purpose |
|---|---|---|
| `OTEL_EXPORTER_OTLP_ENDPOINT` | empty | collector base URL; empty disables export |
| `OTEL_EXPORTER_OTLP_METRICS_ENDPOINT` | empty | signal-specific override; wins over the base |
| `OTEL_EXPORTER_OTLP_PROTOCOL` | `http/protobuf` | only HTTP/protobuf is supported |
| `OTEL_SERVICE_NAME` | `kokoro-tone` | `service.name` resource attribute |
| `OTEL_RESOURCE_ATTRIBUTES` | `deployment.environment.name=homelab` in compose | extra resource attributes |
| `OTEL_METRIC_EXPORT_INTERVAL` | `15000` | export interval in milliseconds, clamped to 1000…300000 |
| `OTEL_EXPORTER_OTLP_TIMEOUT` | `5000` | export timeout in milliseconds, clamped to 1000…60000 |
| `OTEL_SDK_DISABLED` | `false` | `true` keeps the instruments but starts no exporter |

The endpoint is the collector's base URL, for example
`http://otel-collector:4318`; the exporter appends `/v1/metrics`. If you set the
signal-specific variable, give it the full metrics URL.

If the collector is down, the gateway keeps serving. Export failures are logged
and dropped; they never fail a speech request.

## Instruments

OTel names use dots and units; the Prometheus exporter adds `_seconds`,
`_bytes`, `_total`, `_bucket`, `_sum` and `_count`.

| OTel instrument | Type | Prometheus | Attributes |
|---|---|---|---|
| `http.server.request.duration` | histogram `s` | `http_server_request_duration_seconds` | method, status, route, scheme, address, port, streaming |
| `http.server.active_requests` | up/down counter | `http_server_active_requests` | method, scheme, address, port |
| `kokoro_tone.errors` | counter | `kokoro_tone_errors_total` | `error.type` |
| `kokoro_tone.speech.requests` | counter | `kokoro_tone_speech_requests_total` | passthrough, stream_format, format |
| `kokoro_tone.speech.duration` | histogram `s` | `kokoro_tone_speech_duration_seconds` | — |
| `kokoro_tone.speech.audio.duration` | histogram `s` | `kokoro_tone_speech_audio_duration_seconds` | — |
| `kokoro_tone.speech.chars` | histogram `1` | `kokoro_tone_speech_chars` | passthrough |
| `kokoro_tone.upstream.request.duration` | histogram `s` | `kokoro_tone_upstream_request_duration_seconds` | mode, status |
| `kokoro_tone.upstream.up` | gauge | `kokoro_tone_upstream_up` | — |
| `kokoro_tone.ffmpeg.failures` | counter | `kokoro_tone_ffmpeg_failures_total` | stage |
| `kokoro_tone.ffmpeg.duration` | histogram `s` | `kokoro_tone_ffmpeg_duration_seconds` | stage |
| `kokoro_tone.loudness.gain` | histogram `1` | `kokoro_tone_loudness_gain` | dB |
| `kokoro_tone.true_peak` | histogram `1` | `kokoro_tone_true_peak` | dBTP |
| `kokoro_tone.lexicon.problems` | gauge | `kokoro_tone_lexicon_problems` | — |
| `kokoro_tone.build.info` | gauge | `kokoro_tone_build_info` | service.version, runtime, model |

The set is intentionally small. Total request counts come from the HTTP
histogram's `_count`; upstream call counts come from
`kokoro_tone_upstream_request_duration_seconds_count`; ffmpeg invocation counts
come from `kokoro_tone_ffmpeg_duration_seconds_count`. The one counter that
repeats a total is `kokoro_tone.speech.requests`, because it carries the
passthrough/stream/format split that the HTTP metrics do not. No ratio or
average is stored separately.

`http.server.request.duration` measures the whole response, so for passthrough
and SSE it includes the time the client spends reading. Use
`kokoro_tone.speech.duration` for render cost.

## Cardinality and privacy

Attribute values are bounded: route templates, methods, statuses, formats,
ffmpeg stages, upstream modes and error classes. A request to an unknown path is
labelled `route="unmatched"`, never the raw path.

The following are deliberately absent:

- voice names and aliases (client-supplied and effectively unbounded);
- request text, rewritten text, pronunciation rules and sense tags;
- client address, bearer token, request id;
- traces and OTel logs.

A metric is for the operator; the script belongs to the caller.

## Collector and Prometheus

Run `otel/opentelemetry-collector-contrib` on the shared `metrics` network. A
minimal config:

```yaml
receivers:
  otlp:
    protocols:
      http:
        endpoint: 0.0.0.0:4318
processors:
  memory_limiter:
    check_interval: 1s
    limit_mib: 128
  batch:
    timeout: 5s
    send_batch_size: 1024
exporters:
  prometheus:
    endpoint: 0.0.0.0:8889
    resource_to_telemetry_conversion:
      enabled: true
service:
  pipelines:
    metrics:
      receivers: [otlp]
      processors: [memory_limiter, batch]
      exporters: [prometheus]
```

Prometheus scrapes the collector, not the gateway:

```yaml
- job_name: "kokoro-tone"
  metrics_path: /metrics
  static_configs:
    - targets: ["otel-collector:8889"]
```

Metrics are opt-in. The default compose file assumes neither the collector nor
the `metrics` network; `compose.yml` carries a commented reference block. To
enable them, create the shared network once, uncomment the OTel environment and
`networks` entries in `compose.yml`, and start the stack:

```bash
docker network create metrics
docker compose up -d
```

The collector and Prometheus must join the same network.

## Alert examples

```promql
# Engine unreachable for two minutes
kokoro_tone_upstream_up == 0

# More than 5% server errors over five minutes
sum(rate(http_server_request_duration_seconds_count{job="kokoro-tone",http_response_status_code=~"5.."}[5m]))
  / sum(rate(http_server_request_duration_seconds_count{job="kokoro-tone"}[5m])) > 0.05

# Render p95 above ten seconds
histogram_quantile(0.95,
  sum by (le) (rate(kokoro_tone_speech_duration_seconds_bucket{job="kokoro-tone"}[5m]))) > 10

# Any ffmpeg failure in five minutes
increase(kokoro_tone_ffmpeg_failures_total{job="kokoro-tone"}[5m]) > 0

# Mastering gain drifting away from the target
abs(sum(rate(kokoro_tone_loudness_gain_sum[15m])) / sum(rate(kokoro_tone_loudness_gain_count[15m]))) > 12

# Container working set near its 512 MB limit
container_memory_working_set_bytes{name="kokoro-tone"}
  / container_spec_memory_limit_bytes{name="kokoro-tone"} > 0.9
```

## Adding a metric

1. Create the instrument in `telemetry.py`; add a `View` if it is a histogram.
2. Record it at the seam that owns the measurement, not in the route when a
   lower layer already knows the value.
3. Keep the attribute set bounded and document it in the table above.
4. Test it with `InMemoryMetricReader`; no collector is needed.
5. Do not add a counter for a count a histogram already exposes, or a ratio
   that can be derived from two existing instruments.
