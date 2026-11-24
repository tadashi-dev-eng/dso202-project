"""OpenTelemetry distributed tracing.

Manual SDK setup plus library instrumentation (FastAPI server spans, httpx
client spans with W3C ``traceparent`` propagation, asyncpg and redis spans).
Spans go over OTLP/gRPC to the in-cluster OpenTelemetry Collector, which
batches them and exports to Tempo. NATS messages carry the same context in
their headers (see events.py), so one trace follows a request across the
asynchronous hop too.

If ``OTEL_EXPORTER_OTLP_ENDPOINT`` is unset (e.g. a unit test), tracing is a
no-op and the service still runs.
"""

import logging

from opentelemetry import trace
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

from . import config

log = logging.getLogger(__name__)


def setup_tracing(app) -> None:
    endpoint = config.env("OTEL_EXPORTER_OTLP_ENDPOINT")
    if not endpoint:
        log.info("tracing disabled (OTEL_EXPORTER_OTLP_ENDPOINT not set)")
        return

    from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
        OTLPSpanExporter,
    )
    from opentelemetry.instrumentation.asyncpg import AsyncPGInstrumentor
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
    from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor
    from opentelemetry.instrumentation.redis import RedisInstrumentor

    resource = Resource.create(
        {
            "service.name": config.SERVICE_NAME,
            "service.version": config.SERVICE_VERSION,
            "service.namespace": "taskflow",
            "k8s.namespace.name": config.NAMESPACE,
            "k8s.pod.name": config.POD_NAME,
        }
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=endpoint, insecure=True))
    )
    trace.set_tracer_provider(provider)

    FastAPIInstrumentor.instrument_app(
        app, excluded_urls="healthz,readyz,metrics", tracer_provider=provider
    )
    HTTPXClientInstrumentor().instrument(tracer_provider=provider)
    AsyncPGInstrumentor().instrument(tracer_provider=provider)
    RedisInstrumentor().instrument(tracer_provider=provider)
    log.info("tracing enabled", extra={"fields": {"otlp_endpoint": endpoint}})


def tracer():
    return trace.get_tracer(config.SERVICE_NAME)
