"""Structured JSON logging with trace correlation.

One JSON object per line on stdout. The node's log agent ships stdout to
Loki unchanged, and Grafana links a log line to its trace through the
``trace_id`` field (derived field on the Loki data source).
"""

import json
import logging
import sys
import time

from opentelemetry import trace

from . import config


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        span = trace.get_current_span().get_span_context()
        doc = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created))
            + f".{int(record.msecs):03d}Z",
            "level": record.levelname.lower(),
            "service": config.SERVICE_NAME,
            "pod": config.POD_NAME,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if span.is_valid:
            doc["trace_id"] = format(span.trace_id, "032x")
            doc["span_id"] = format(span.span_id, "016x")
        # Extra structured fields: log.info("...", extra={"fields": {...}})
        fields = getattr(record, "fields", None)
        if isinstance(fields, dict):
            doc.update(fields)
        if record.exc_info:
            doc["exc"] = self.formatException(record.exc_info)
        return json.dumps(doc, default=str)


def setup_logging() -> logging.Logger:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(config.LOG_LEVEL)
    # uvicorn's own loggers would otherwise print plain text.
    for name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        lg = logging.getLogger(name)
        lg.handlers[:] = []
        lg.propagate = True
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return logging.getLogger(config.SERVICE_NAME)
