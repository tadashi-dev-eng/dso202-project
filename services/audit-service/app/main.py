"""audit-service -- immutable audit log of every change.

Consumes everything under ``taskflow.>`` and appends one row per event.

* **Immutable**: a trigger rejects UPDATE and DELETE on ``audit_log``, so
  not even the service's own database role can rewrite history.
* **Exactly one row per event** despite at-least-once delivery: the
  event's id is unique and duplicates are ignored (``ON CONFLICT DO
  NOTHING``).

Reading: ``GET /audit?entity_id=<task>`` (access checked through
task-service) and ``GET /audit/me`` (the caller's own actions).
"""

import json
import uuid
from datetime import datetime, timezone

from fastapi import Query
from prometheus_client import Counter

from taskflow_common import create_service
from taskflow_common.access import require_task
from taskflow_common.auth import CurrentUser
from taskflow_common.users import display_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    seq         BIGSERIAL PRIMARY KEY,
    event_id    UUID NOT NULL UNIQUE,
    event_type  TEXT NOT NULL,
    source      TEXT NOT NULL,
    entity_type TEXT,
    entity_id   UUID,
    list_id     UUID,
    actor_id    UUID,
    occurred_at TIMESTAMPTZ NOT NULL,
    recorded_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    payload     JSONB NOT NULL
);
CREATE INDEX IF NOT EXISTS audit_entity ON audit_log(entity_id, occurred_at);
CREATE INDEX IF NOT EXISTS audit_actor ON audit_log(actor_id, occurred_at);

CREATE OR REPLACE FUNCTION audit_log_immutable() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append-only (% rejected)', TG_OP;
END $$ LANGUAGE plpgsql;
DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log;
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_immutable();
"""

RECORDED = Counter("taskflow_audit_events_total", "Audit rows written", ["event"])


def _entity(subject: str, data: dict) -> tuple[str | None, str | None, str | None]:
    """(entity_type, entity_id, list_id) for any TaskFlow event."""
    kind = subject.split(".")[0]
    if "task" in data and isinstance(data["task"], dict):
        t = data["task"]
        return "task", t.get("id"), t.get("list_id")
    if "comment" in data:
        return "task", data.get("task_id"), data.get("list_id")
    if "attachment" in data:
        return "task", data["attachment"].get("task_id"), None
    if "list" in data and isinstance(data["list"], dict):
        return "list", data["list"].get("id"), data["list"].get("id")
    if "task_id" in data:
        return "task", data["task_id"], data.get("list_id")
    if "list_id" in data:
        return "list", data["list_id"], data["list_id"]
    return kind, None, None


def _uuid(value):
    try:
        return uuid.UUID(value) if value else None
    except (ValueError, TypeError):
        return None


async def _handle(subject: str, event: dict) -> None:
    data = event.get("data", {})
    etype, eid, list_id = _entity(subject, data)
    await ctx.db.execute(
        "INSERT INTO audit_log (event_id, event_type, source, entity_type, entity_id, list_id, "
        "actor_id, occurred_at, payload) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9::jsonb) "
        "ON CONFLICT (event_id) DO NOTHING",
        uuid.UUID(event["id"]), subject, event.get("source", "?"), etype, _uuid(eid),
        _uuid(list_id), _uuid(data.get("actor_id") or data.get("user_id")),
        datetime.fromtimestamp(event.get("time", 0), timezone.utc), json.dumps(data),
    )
    RECORDED.labels(subject).inc()


async def _startup(c) -> None:
    c.bus.consume("audit-service", "taskflow.>", _handle, batch=50)


app, ctx = create_service("audit-service", schema=SCHEMA, use_nats=True, on_startup=_startup)


async def _rows_out(rows) -> list[dict]:
    out = []
    for r in rows:
        actor = str(r["actor_id"]) if r["actor_id"] else None
        out.append({
            "id": r["seq"], "event_type": r["event_type"], "source": r["source"],
            "entity_type": r["entity_type"],
            "entity_id": str(r["entity_id"]) if r["entity_id"] else None,
            "actor_id": actor, "actor_name": await display_name(ctx, actor),
            "occurred_at": r["occurred_at"].isoformat(),
        })
    return out


@app.get("/audit")
async def entity_history(entity_id: uuid.UUID, user: CurrentUser,
                         limit: int = Query(default=100, le=500)):
    await require_task(ctx, entity_id, user)
    rows = await ctx.db.fetch(
        "SELECT * FROM audit_log WHERE entity_id = $1 ORDER BY occurred_at DESC LIMIT $2",
        entity_id, limit,
    )
    return await _rows_out(rows)


@app.get("/audit/me")
async def my_history(user: CurrentUser, limit: int = Query(default=100, le=500)):
    rows = await ctx.db.fetch(
        "SELECT * FROM audit_log WHERE actor_id = $1 ORDER BY occurred_at DESC LIMIT $2",
        uuid.UUID(user), limit,
    )
    return await _rows_out(rows)
