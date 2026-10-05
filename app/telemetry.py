import logging
import os

from opentelemetry import metrics, trace
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor, ConsoleLogExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import ConsoleMetricExporter, PeriodicExportingMetricReader
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter


SERVICE_NAME = "order-tracker"


def configure_telemetry():
    """Send metrics, logs and traces to the console, or to an OTLP collector.

    TELEMETRY_EXPORTER=console (default) prints signals to stdout.
    TELEMETRY_EXPORTER=otlp sends them to OTEL_EXPORTER_OTLP_ENDPOINT.
    """
    resource = Resource.create({"service.name": SERVICE_NAME})
    use_otlp = os.getenv("TELEMETRY_EXPORTER", "console") == "otlp"

    tracer_provider = TracerProvider(resource=resource)
    logger_provider = LoggerProvider(resource=resource)
    if use_otlp:
        from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
        from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        span_exporter = OTLPSpanExporter()
        log_exporter = OTLPLogExporter()
        metric_exporter = OTLPMetricExporter()
    else:
        span_exporter = ConsoleSpanExporter()
        log_exporter = ConsoleLogExporter()
        metric_exporter = ConsoleMetricExporter()

    tracer_provider.add_span_processor(BatchSpanProcessor(span_exporter))
    logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[PeriodicExportingMetricReader(metric_exporter, export_interval_millis=5000)],
    )

    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)

    # Send standard Python logs (including uvicorn's) through OpenTelemetry too.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger().addHandler(LoggingHandler(level=logging.INFO, logger_provider=logger_provider))


tracer = trace.get_tracer(SERVICE_NAME)
meter = metrics.get_meter(SERVICE_NAME)
logger = logging.getLogger("order_tracker")

# Request metric: one data point per route, method and HTTP status code.
request_counter = meter.create_counter(
    "http.server.requests",
    unit="{request}",
    description="Count of HTTP requests handled by the API",
)
request_duration = meter.create_histogram(
    "http.server.duration",
    unit="s",
    description="Duration of HTTP requests handled by the API",
)
