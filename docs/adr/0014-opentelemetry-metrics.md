# OpenTelemetry metrics through a collector

**Status:** accepted (implemented)
**Date:** 2026-11-12
**Author:** sys-admin
**Refs:** [`../observability.md`](../observability.md),
[`../../src/kokoro_tone/telemetry.py`](../../src/kokoro_tone/telemetry.py)

## Context

The gateway is stateless: it owns no queue, no database and no model. What an
operator needs to see is therefore the work of one request - characters, turns,
engine calls, ffmpeg stages, render time, produced audio, mastering gain - and
the health of the engine behind it.

The deployment already runs Prometheus, Grafana, Loki and cAdvisor. Logs are
logfmt and already carry `chars`, `turns`, `upstream_ms`, `render_ms` and
`bytes`; cAdvisor covers container CPU, memory and restarts. What is missing is
an aggregate, alertable view of the application.

The question is not whether to instrument, but where the metric contract lives
and how it reaches Prometheus.

## Decision

Instrument with the OpenTelemetry API and export **OTLP/HTTP/protobuf** to an
OpenTelemetry Collector. The collector exposes a Prometheus endpoint; the
gateway has no `/metrics` route and no Prometheus client library.

- One `MeterProvider` is built per app in `create_app`, with a `Resource` that
  carries `service.name`, `service.version` and `service.instance.id`.
- HTTP server metrics use the OpenTelemetry semantic conventions
  (`http.server.request.duration`, `http.server.active_requests`).
- Domain metrics use the `kokoro_tone.*` namespace with units in the
  instrument, not in the name.
- Histogram boundaries are explicit `View`s. The SDK defaults are
  millisecond-oriented and wrong for seconds, audio duration and gain.
- The set is deliberately small: no counter that duplicates a histogram's
  count, no ratio derived from two durations, and no configuration gauge with
  no action. Request and upstream counts come from histogram `_count`.
- Attributes are bounded: route templates, methods, statuses, formats, ffmpeg
  stages and error classes. Voice, alias, request text, client address and
  request id are never attributes.
- Export is best-effort. A collector outage must not fail a speech request; the
  exporter drops the interval and the app keeps serving.
- The collector joins the shared `metrics` network with the gateway and
  Prometheus. Metrics are opt-in: `compose.yml` carries a commented reference
  block, and the default build assumes neither the network nor the collector.

Traces and OTel logs are deliberately out of scope. The resource and collector
make them cheap to add later; they are not needed to answer the questions this
change is for.

## Consequences

- A collector becomes part of the observability path. The gateway degrades
  gracefully without it, but there are no metrics until it is running.
- The Prometheus names are produced by the collector's exporter, so the
  `_seconds`, `_bytes`, `_total`, `_bucket`, `_sum` and `_count` suffixes are
  not written by hand. The OTel name is the contract; the Prometheus name is
  documented in [`../observability.md`](../observability.md).
- Metrics arrive on the export interval (15 s by default), not instantly. The
  interval is configurable with `OTEL_METRIC_EXPORT_INTERVAL`.
- The OTLP/HTTP exporter adds protobuf and a small HTTP client to the image.
  gRPC was rejected for size; the gateway does not need it.
- The collector can convert resource attributes into labels, which makes
  `service.version` joinable but also adds `service.instance.id` to every
  series. Prometheus handles the restart churn; dashboards should aggregate by
  `service.name`.

## Considered options

**Use `prometheus-client` and expose `/metrics` directly.** Rejected: it
couples the app to one backend, loses the OTel resource and semantic
conventions, and would need a second instrumentation path if traces are added
later.

**Use the in-process OTel Prometheus exporter.** Rejected for this deployment:
it is a legitimate OTel pattern and smaller, but it keeps the scrape contract
inside the app and gives no place for a shared collector. It remains the
fallback if image size or collector operations become a problem.

**Auto-instrument FastAPI and call it done.** Rejected: the HTTP
instrumentation is primarily tracing, its metric coverage varies by version,
and the domain metrics still have to be written by hand. The gateway records
HTTP metrics in its own middleware so the route label and streaming attribute
are explicit.

**StatsD or log-derived metrics.** Rejected: log-derived metrics duplicate the
event line and cannot express histograms cleanly; StatsD adds a second
transport for no benefit over OTLP.

**Add traces and OTel logs at the same time.** Rejected as scope. The metrics
answer the capacity and health questions; request-level causality can be a
separate change on the same resource and collector.
