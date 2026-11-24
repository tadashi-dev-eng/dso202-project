"""Unit tests for the shared service template and for pure service logic.

Run (CI does this in the "Test" stage):
    pip install -r services/requirements.txt pytest
    REGISTRY_ENABLED=false PYTHONPATH=services/common pytest -q services/tests
"""

import functools
import importlib.util
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
os.environ.setdefault("REGISTRY_ENABLED", "false")


@functools.cache
def load_service(name: str):
    """Import services/<name>/app/main.py under a unique module name (every
    service's package is called "app", so a plain import would collide).
    Cached: importing twice would register its Prometheus metrics twice."""
    path = ROOT / name / "app" / "main.py"
    spec = importlib.util.spec_from_file_location(f"svc_{name.replace('-', '_')}", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


# --- taskflow_common ---------------------------------------------------------

def test_registry_prefix_end_covers_every_key_under_prefix():
    from taskflow_common.registry import _prefix_end

    end = _prefix_end("/taskflow/services/task-service/")
    assert end == "/taskflow/services/task-service0"
    assert "/taskflow/services/task-service/pod-a" < end
    assert "/taskflow/services/task-servicez" > end


def test_service_url_default_and_override(monkeypatch):
    from taskflow_common import config

    assert config.service_url("list-service") == "http://list-service:8000"
    monkeypatch.setenv("LIST_SERVICE_URL", "http://10.0.0.7:8000")
    assert config.service_url("list-service") == "http://10.0.0.7:8000"


def test_probes_and_metrics_use_route_templates():
    from fastapi.testclient import TestClient
    from taskflow_common import create_service

    app, _ = create_service("unit-test-service", use_db=False)

    @app.get("/items/{item_id}")
    async def item(item_id: int):
        return {"id": item_id}

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        assert client.get("/readyz").status_code == 200
        for i in range(3):
            client.get(f"/items/{i}")
        metrics = client.get("/metrics").text
    # one series for the template, not one per id (bounded cardinality)
    assert 'route="/items/{item_id}"' in metrics
    assert 'route="/items/1"' not in metrics
    # probes are not counted as traffic
    assert 'route="/healthz"' not in metrics


def test_readiness_reports_failing_dependency():
    from fastapi.testclient import TestClient
    from taskflow_common import create_service

    app, ctx = create_service("unit-test-ready", use_db=False)

    async def broken():
        raise RuntimeError("database down")

    with TestClient(app) as client:
        ctx.checks["postgres"] = broken
        resp = client.get("/readyz")
    assert resp.status_code == 503
    assert resp.json()["failed"] == {"postgres": "database down"}


def test_missing_identity_is_rejected():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from taskflow_common.auth import CurrentUser

    app = FastAPI()

    @app.get("/me")
    async def me(user: CurrentUser):
        return {"user": user}

    client = TestClient(app)
    assert client.get("/me").status_code == 401
    assert client.get("/me", headers={"X-User-Id": "u1"}).json() == {"user": "u1"}


# --- service logic -----------------------------------------------------------

@pytest.mark.parametrize("frequency,base,expected", [
    ("daily", datetime(2026, 11, 30, 9, tzinfo=timezone.utc), datetime(2026, 12, 1, 9, tzinfo=timezone.utc)),
    ("weekly", datetime(2026, 11, 30, 9, tzinfo=timezone.utc), datetime(2026, 12, 7, 9, tzinfo=timezone.utc)),
    ("monthly", datetime(2026, 1, 31, 9, tzinfo=timezone.utc), datetime(2026, 2, 28, 9, tzinfo=timezone.utc)),
    ("monthly", datetime(2026, 12, 15, 9, tzinfo=timezone.utc), datetime(2027, 1, 15, 9, tzinfo=timezone.utc)),
])
def test_recurrence_next_occurrence(frequency, base, expected):
    recurrence = load_service("recurrence-service")
    assert recurrence._next(base, frequency) == expected


def test_audit_maps_every_event_shape_to_an_entity():
    audit = load_service("audit-service")
    task = {"id": "t1", "list_id": "l1"}
    assert audit._entity("task.created", {"task": task}) == ("task", "t1", "l1")
    assert audit._entity("comment.created",
                         {"comment": {"id": "c1"}, "task_id": "t1", "list_id": "l1"}) == ("task", "t1", "l1")
    assert audit._entity("list.created", {"list": {"id": "l1"}}) == ("list", "l1", "l1")
    assert audit._entity("list.deleted", {"list_id": "l1"}) == ("list", "l1", "l1")
    assert audit._entity("reminder.due", {"task_id": "t1"}) == ("task", "t1", None)


def test_gateway_routes_cover_every_backend_service():
    charts = ROOT.parent / "charts" / "taskflow" / "charts"
    backends = {p.name for p in charts.iterdir()
                if p.is_dir() and p.name not in ("taskflow-lib", "web-frontend", "api-gateway")}
    source = (ROOT / "api-gateway" / "app" / "main.py").read_text()
    for service in backends:
        assert f'"{service}"' in source, f"gateway has no route to {service}"
