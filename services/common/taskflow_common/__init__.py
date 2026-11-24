"""taskflow_common -- the shared service template for TaskFlow.

Every backend service is a thin FastAPI app built with ``create_service``.
The template supplies everything the platform expects from a workload, so
each service only writes its own routes:

* JSON logs carrying ``trace_id``          (logging_setup) -> Loki
* RED metrics on ``/metrics``              (metrics)       -> Prometheus
* OpenTelemetry traces over OTLP           (tracing)       -> Tempo
* ``/healthz`` and ``/readyz`` probes      (app)
* registration in the etcd service registry (registry)
* PostgreSQL pool with startup retry       (db)
* NATS JetStream publish / consume         (events)
* the caller's identity set by the gateway (auth)
"""

from .app import ServiceContext, create_service  # noqa: F401
