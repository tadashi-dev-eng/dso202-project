"""recurrence-service -- generates recurring tasks (daily, weekly, monthly).

A rule is attached to one task. When that task is completed
(``task.completed``), the service creates the next occurrence through
task-service's internal API, on behalf of the user who completed it, and
moves the rule onto the new task. The move happens in the same database
transaction as the read, so a redelivered event finds no rule and creates
nothing twice.
"""

import calendar
import uuid
from datetime import datetime, timedelta, timezone
from typing import Literal

from fastapi import HTTPException
from prometheus_client import Counter
from pydantic import BaseModel

from taskflow_common import create_service
from taskflow_common.access import require_task
from taskflow_common.auth import CurrentUser

SCHEMA = """
CREATE TABLE IF NOT EXISTS rules (
    task_id    UUID PRIMARY KEY,
    user_id    UUID NOT NULL,
    frequency  TEXT NOT NULL CHECK (frequency IN ('daily', 'weekly', 'monthly')),
    generated  INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

GENERATED = Counter("taskflow_recurrences_generated_total", "Occurrences created", ["frequency"])


def _next(base: datetime, frequency: str) -> datetime:
    if frequency == "daily":
        return base + timedelta(days=1)
    if frequency == "weekly":
        return base + timedelta(weeks=1)
    month = base.month % 12 + 1
    year = base.year + (base.month == 12)
    day = min(base.day, calendar.monthrange(year, month)[1])
    return base.replace(year=year, month=month, day=day)


async def _on_completed(subject: str, event: dict) -> None:
    task = event["data"]["task"]
    async with ctx.db.acquire() as conn, conn.transaction():
        rule = await conn.fetchrow(
            "DELETE FROM rules WHERE task_id = $1 RETURNING *", uuid.UUID(task["id"])
        )
        if rule is None:
            return
        base = datetime.fromisoformat(task["due_at"]) if task.get("due_at") \
            else datetime.now(timezone.utc)
        due = _next(base, rule["frequency"])
        actor = event["data"].get("actor_id") or str(rule["user_id"])
        resp = await ctx.call("task-service", "POST", "/internal/tasks", user_id=actor, json={
            "list_id": task["list_id"], "title": task["title"],
            "description": task.get("description", ""), "priority": task["priority"],
            "due_at": due.isoformat(), "recurrence_of": task["id"],
        })
        resp.raise_for_status()  # raising rolls back: the event is redelivered
        new_task = resp.json()
        await conn.execute(
            "INSERT INTO rules (task_id, user_id, frequency, generated) VALUES ($1, $2, $3, $4)",
            uuid.UUID(new_task["id"]), rule["user_id"], rule["frequency"], rule["generated"] + 1,
        )
    GENERATED.labels(rule["frequency"]).inc()
    await ctx.bus.publish("recurrence.generated", {
        "from_task_id": task["id"], "task": new_task, "frequency": rule["frequency"],
        "actor_id": actor,
    })
    ctx.log.info("next occurrence created",
                 extra={"fields": {"from": task["id"], "to": new_task["id"]}})


async def _on_deleted(subject: str, event: dict) -> None:
    await ctx.db.execute("DELETE FROM rules WHERE task_id = $1",
                         uuid.UUID(event["data"]["task"]["id"]))


async def _startup(c) -> None:
    c.bus.consume("recurrence-service-completed", "taskflow.task.completed", _on_completed)
    c.bus.consume("recurrence-service-deleted", "taskflow.task.deleted", _on_deleted)


app, ctx = create_service("recurrence-service", schema=SCHEMA, use_nats=True,
                          on_startup=_startup)


class Rule(BaseModel):
    frequency: Literal["daily", "weekly", "monthly"]


def _out(row) -> dict:
    return {"task_id": str(row["task_id"]), "frequency": row["frequency"],
            "generated": row["generated"]}


@app.get("/recurrences/task/{task_id}")
async def get_rule(task_id: uuid.UUID, user: CurrentUser):
    await require_task(ctx, task_id, user)
    row = await ctx.db.fetchrow("SELECT * FROM rules WHERE task_id = $1", task_id)
    return _out(row) if row else None


@app.put("/recurrences/task/{task_id}")
async def set_rule(task_id: uuid.UUID, body: Rule, user: CurrentUser):
    await require_task(ctx, task_id, user, write=True)
    row = await ctx.db.fetchrow(
        "INSERT INTO rules (task_id, user_id, frequency) VALUES ($1, $2, $3) "
        "ON CONFLICT (task_id) DO UPDATE SET frequency = EXCLUDED.frequency RETURNING *",
        task_id, uuid.UUID(user), body.frequency,
    )
    return _out(row)


@app.delete("/recurrences/task/{task_id}", status_code=204)
async def delete_rule(task_id: uuid.UUID, user: CurrentUser):
    await require_task(ctx, task_id, user, write=True)
    done = await ctx.db.execute("DELETE FROM rules WHERE task_id = $1", task_id)
    if done.endswith(" 0"):
        raise HTTPException(404, "no rule")
