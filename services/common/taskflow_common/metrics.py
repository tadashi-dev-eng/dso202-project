"""RED metrics (rate, errors, duration) for every HTTP request.

The ``route`` label is the route *template* (``/tasks/{task_id}``), never
the raw path, so the number of time series stays bounded no matter how many
ids are requested. Prometheus scrapes ``/metrics`` through the
ServiceMonitor that the Helm library chart creates for each service.

These two series feed the application dashboard and the HPA:
``http_server_requests_total`` -> requests/second per pod (Prometheus
Adapter turns it into the ``http_requests_per_second`` custom metric).
"""

import time

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from . import config

REQUESTS = Counter(
    "http_server_requests_total",
    "HTTP requests handled",
    ["service", "method", "route", "status"],
)
DURATION = Histogram(
    "http_server_request_duration_seconds",
    "HTTP request latency",
    ["service", "method", "route"],
    buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
)
IN_FLIGHT = Gauge(
    "http_server_requests_in_flight", "Requests being handled", ["service"]
)
BUILD_INFO = Gauge(
    "taskflow_build_info", "Service build information", ["service", "version"]
)

# Probe and scrape endpoints are excluded so they do not inflate the request
# rate that the autoscaler acts on.
_SKIP = {"/metrics", "/healthz", "/readyz"}


class MetricsMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path in _SKIP:
            return await call_next(request)
        svc = config.SERVICE_NAME
        IN_FLIGHT.labels(svc).inc()
        start = time.perf_counter()
        status = 500
        try:
            response = await call_next(request)
            status = response.status_code
            return response
        finally:
            route = request.scope.get("route")
            template = getattr(route, "path", None) or "unmatched"
            REQUESTS.labels(svc, request.method, template, str(status)).inc()
            DURATION.labels(svc, request.method, template).observe(
                time.perf_counter() - start
            )
            IN_FLIGHT.labels(svc).dec()


async def metrics_endpoint(_: Request) -> Response:
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
