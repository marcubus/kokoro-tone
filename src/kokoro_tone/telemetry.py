"""OpenTelemetry metrics for one gateway process.

The gateway is stateless, so the useful signals are work per request, the
engine's latency, ffmpeg's cost and the mastering result. Instruments are
created here and recorded at the seam that owns the measurement; this module
does not import the HTTP layer or the audio chain.

Export is OTLP/HTTP to a collector. The collector owns the Prometheus endpoint,
so the gateway never exposes a metrics port of its own. A deployment with no
endpoint still gets a working app: the meter provider has no reader and every
instrument is a cheap no-op.

The set is deliberately small. A counter that duplicates a histogram's count,
a ratio derived from two durations, or a configuration gauge with no action is
not a metric; it is dashboard decoration. Attributes are bounded for the same
reason: no request text, voice name, alias, client address or request id.
"""

from __future__ import annotations

import contextlib
import platform
import socket

from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.metrics import Meter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import MetricReader, PeriodicExportingMetricReader
from opentelemetry.sdk.metrics.view import ExplicitBucketHistogramAggregation, View
from opentelemetry.sdk.resources import Resource

from . import __version__
from .settings import Settings

SERVICE_NAME = "service.name"
SERVICE_VERSION = "service.version"
SERVICE_INSTANCE_ID = "service.instance.id"

# Histogram boundaries. OTel's defaults are millisecond-oriented, which is
# wrong for seconds and for audio-sized values, so every histogram gets an
# explicit view.
_SECONDS = (0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30, 60, 120)
_AUDIO_SECONDS = (0.1, 0.5, 1, 2, 5, 10, 30, 60, 120, 300, 600)
_CHARS = (1, 16, 64, 128, 256, 512, 1024, 2048, 4096)
_GAIN_DB = (-30, -20, -15, -10, -5, -2, 0, 2, 5, 10, 15, 20, 30)
_PEAK_DB = (-30, -20, -12, -6, -3, -1, 0)


def _view(name: str, boundaries: tuple[float, ...]) -> View:
    return View(
        instrument_name=name,
        aggregation=ExplicitBucketHistogramAggregation(boundaries=boundaries),
    )


VIEWS: tuple[View, ...] = (
    _view("http.server.request.duration", _SECONDS),
    _view("kokoro_tone.speech.duration", _SECONDS),
    _view("kokoro_tone.speech.audio.duration", _AUDIO_SECONDS),
    _view("kokoro_tone.speech.chars", _CHARS),
    _view("kokoro_tone.upstream.request.duration", _SECONDS),
    _view("kokoro_tone.ffmpeg.duration", _SECONDS),
    _view("kokoro_tone.loudness.gain", _GAIN_DB),
    _view("kokoro_tone.true_peak", _PEAK_DB),
)


def _resource(service_name: str) -> Resource:
    # Resource.create merges OTEL_RESOURCE_ATTRIBUTES and the process detector,
    # so deployment.environment.name and friends still come from the standard
    # environment. The instance id is the container hostname: stable for the
    # life of the container, different after a recreate.
    return Resource.create(
        {
            SERVICE_NAME: service_name,
            SERVICE_VERSION: __version__,
            SERVICE_INSTANCE_ID: socket.gethostname(),
        }
    )


class Telemetry:
    """The instruments one app records into, and the provider behind them."""

    def __init__(self, meter: Meter, provider: MeterProvider) -> None:
        self.provider = provider
        self.meter = meter

        self.http_server_request_duration = meter.create_histogram(
            "http.server.request.duration",
            unit="s",
            description="Duration of an HTTP server request, including streaming.",
        )
        self.http_server_active_requests = meter.create_up_down_counter(
            "http.server.active_requests",
            unit="{request}",
            description="HTTP requests currently in flight.",
        )
        self.errors = meter.create_counter(
            "kokoro_tone.errors",
            unit="{error}",
            description="Requests that failed, by class of failure.",
        )

        self.speech_requests = meter.create_counter(
            "kokoro_tone.speech.requests",
            unit="{request}",
            description="Speech requests that produced a response.",
        )
        self.speech_duration = meter.create_histogram(
            "kokoro_tone.speech.duration",
            unit="s",
            description="Time spent rendering and encoding one speech request.",
        )
        self.speech_audio_duration = meter.create_histogram(
            "kokoro_tone.speech.audio.duration",
            unit="s",
            description="Duration of the audio produced by one request.",
        )
        self.speech_chars = meter.create_histogram(
            "kokoro_tone.speech.chars",
            unit="1",
            description="Characters in a speech request after rewriting.",
        )

        self.upstream_duration = meter.create_histogram(
            "kokoro_tone.upstream.request.duration",
            unit="s",
            description="Time to the engine's response headers.",
        )
        self.upstream_up = meter.create_gauge(
            "kokoro_tone.upstream.up",
            unit="1",
            description="1 when the last engine interaction succeeded, else 0.",
        )

        self.ffmpeg_failures = meter.create_counter(
            "kokoro_tone.ffmpeg.failures",
            unit="{failure}",
            description="ffmpeg processes that failed, by stage.",
        )
        self.ffmpeg_duration = meter.create_histogram(
            "kokoro_tone.ffmpeg.duration",
            unit="s",
            description="Wall time of one ffmpeg process, by stage.",
        )
        self.loudness_gain = meter.create_histogram(
            "kokoro_tone.loudness.gain",
            unit="1",
            description="Linear mastering gain applied, in dB.",
        )
        self.true_peak = meter.create_histogram(
            "kokoro_tone.true_peak",
            unit="1",
            description="True peak of the tone-shaped file, in dBTP.",
        )

        self.lexicon_problems = meter.create_gauge(
            "kokoro_tone.lexicon.problems",
            unit="1",
            description="Problems found in the deployment lexicon.",
        )
        self.build_info = meter.create_gauge(
            "kokoro_tone.build.info",
            unit="1",
            description="Build identity; the value is always 1.",
        )

    def set_info(self, settings: Settings) -> None:
        """Publish the build identity once, as a joinable info metric."""
        self.build_info.set(
            1,
            {
                "service.version": __version__,
                "process.runtime.name": platform.python_implementation().lower(),
                "process.runtime.version": platform.python_version(),
                "gen_ai.request.model": settings.upstream_model,
            },
        )

    def shutdown(self) -> None:
        """Flush the last interval, then stop the exporter's background thread."""
        with contextlib.suppress(Exception):
            self.provider.force_flush(timeout_millis=5_000)
        with contextlib.suppress(Exception):
            self.provider.shutdown()


def build_telemetry(settings: Settings, *, metric_reader: MetricReader | None = None) -> Telemetry:
    """Build one provider for one app.

    `metric_reader` is the test seam: an `InMemoryMetricReader` collects on
    demand and starts no exporter thread. In production the reader is a
    periodic OTLP/HTTP exporter when an endpoint is configured, and absent
    otherwise.
    """
    readers: list[MetricReader] = []
    if metric_reader is not None:
        readers.append(metric_reader)
    elif not settings.otel_disabled and settings.otel_endpoint:
        exporter = OTLPMetricExporter()
        readers.append(
            PeriodicExportingMetricReader(
                exporter,
                export_interval_millis=settings.otel_export_interval_ms,
                export_timeout_millis=settings.otel_export_timeout_ms,
            )
        )

    provider = MeterProvider(
        metric_readers=readers,
        resource=_resource(settings.otel_service_name),
        views=VIEWS,
        shutdown_on_exit=False,
    )
    return Telemetry(provider.get_meter("kokoro_tone", __version__), provider)


__all__ = ["Telemetry", "build_telemetry", "VIEWS"]
