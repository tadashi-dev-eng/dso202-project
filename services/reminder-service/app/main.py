"""reminder-service -- schedules reminders for upcoming due dates.

Two kinds of reminder:
* manual: the user picks a time in the UI;
* auto: when a task gets a future due date, a reminder is scheduled one
  hour before it for the task's creator (rescheduled if the due date moves,
  cancelled when the task is completed or deleted).

A scheduler loop runs in every replica. It claims due reminders with
``SELECT ... FOR UPDATE SKIP LOCKED``: two replicas never claim the same
row, so the service can run more than one replica (high availability)
without sending a reminder twice. Each claimed reminder is published as
``reminder.due``; notification-service delivers it.
"""

import asyncio
import uuid
from datetime import datetime, timedelta, timezone

from fastapi import HTTPException
from prometheus_client import Counter, Gauge
from pydantic import BaseModel

from taskflow_common import create_service
from taskflow_common.access import require_task
from taskflow_common.auth import CurrentUser
from taskflow_common.config import env_int

SCHEMA = """
CREATE TABLE IF NOT EXISTS reminders (
    id         UUID PRIMARY KEY,
    task_id    UUID NOT NULL,
    user_id    UUID NOT NULL,
    task_title TEXT NOT NULL DEFAULT '',
    remind_at  TIMESTAMPTZ NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'manual' CHECK (kind IN ('manual', 'auto')),
    status     TEXT NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'sent', 'cancelled')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    sent_at    TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS reminders_due ON reminders(remind_at) WHERE status = 'pending';
CREATE UNIQUE INDEX IF NOT EXISTS reminders_auto ON reminders(task_id) WHERE kind = 'auto';
"""

LEAD = timedelta(minutes=env_int("AUTO_REMINDER_LEAD_MINUTES", 60))
TICK = env_int("SCHEDULER_INTERVAL_SECONDS", 5)
FIRED = Counter("taskflow_reminders_fired_total", "Reminders that became due", ["kind"])
PENDING = Gauge("taskflow_reminders_pending", "Reminders waiting to fire")


async def _scheduler(c) -> None:
    while True:
        try:
            async with c.db.acquire() as conn, conn.transaction():
                due = await conn.fetch(
                    "SELECT * FROM reminders WHERE status = 'pending' AND remind_at <= now() "
                    "ORDER BY remind_at LIMIT 100 FOR UPDATE SKIP LOCKED"
                )
                for r in due:
                    await c.bus.publish("reminder.due", {
                        "reminder_id": str(r["id"]), "task_id": str(r["task_id"]),
                        "user_id": str(r["user_id"]), "task_title": r["task_title"],
                        "remind_at": r["remind_at"].isoformat(), "kind": r["kind"],
                    })
                    FIRED.labels(r["kind"]).inc()
                if due:
                    await conn.execute(
                        "UPDATE reminders SET status = 'sent', sent_at = now() "
                        "WHERE id = ANY($1::uuid[])", [r["id"] for r in due],
                    )
            PENDING.set(await c.db.fetchval(
                "SELECT count(*) FROM reminders WHERE status = 'pending'"))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 -- the loop must survive a DB blip
            c.log.exception("scheduler tick failed")
        await asyncio.sleep(TICK)


async def _on_task_event(subject: str, event: dict) -> None:
    task = event["data"]["task"]
    task_id = uuid.UUID(task["id"])
    if subject in ("task.deleted", "task.completed") or task.get("status") == "done":
        await ctx.db.execute(
            "UPDATE reminders SET status = 'cancelled' WHERE task_id = $1 AND status = 'pending'",
            task_id,
        )
        return
    due_raw = task.get("due_at")
    if not due_raw:
        await ctx.db.execute(
            "DELETE FROM reminders WHERE task_id = $1 AND kind = 'auto' AND status = 'pending'",
            task_id,
        )
        return
    due = datetime.fromisoformat(due_raw)
    now = datetime.now(timezone.utc)
    if due <= now:
        return
    remind_at = max(due - LEAD, now)
    await ctx.db.execute(
        "INSERT INTO reminders (id, task_id, user_id, task_title, remind_at, kind) "
        "VALUES ($1, $2, $3, $4, $5, 'auto') "
        "ON CONFLICT (task_id) WHERE kind = 'auto' DO UPDATE SET remind_at = EXCLUDED.remind_at, "
        "task_title = EXCLUDED.task_title, status = 'pending', sent_at = NULL",
        uuid.uuid4(), task_id, uuid.UUID(task["created_by"]), task["title"], remind_at,
    )


async def _startup(c) -> None:
    for subject in ("created", "updated", "completed", "deleted"):
        c.bus.consume(f"reminder-service-task-{subject}", f"taskflow.task.{subject}",
                      _on_task_event)
    c.state["scheduler"] = asyncio.create_task(_scheduler(c))


async def _shutdown(c) -> None:
    c.state["scheduler"].cancel()


app, ctx = create_service("reminder-service", schema=SCHEMA, use_nats=True,
                          on_startup=_startup, on_shutdown=_shutdown)


class NewReminder(BaseModel):
    task_id: uuid.UUID
    remind_at: datetime


def _out(row) -> dict:
    return {
        "id": str(row["id"]),
        "task_id": str(row["task_id"]),
        "remind_at": row["remind_at"].isoformat(),
        "kind": row["kind"],
        "status": row["status"],
    }


@app.get("/reminders")
async def list_reminders(task_id: uuid.UUID, user: CurrentUser):
    await require_task(ctx, task_id, user)
    rows = await ctx.db.fetch(
        "SELECT * FROM reminders WHERE task_id = $1 AND user_id = $2 AND status <> 'cancelled' "
        "ORDER BY remind_at", task_id, uuid.UUID(user),
    )
    return [_out(r) for r in rows]


@app.post("/reminders", status_code=201)
async def add_reminder(body: NewReminder, user: CurrentUser):
    acc = await require_task(ctx, body.task_id, user)
    when = body.remind_at if body.remind_at.tzinfo else body.remind_at.replace(tzinfo=timezone.utc)
    row = await ctx.db.fetchrow(
        "INSERT INTO reminders (id, task_id, user_id, task_title, remind_at) "
        "VALUES ($1, $2, $3, $4, $5) RETURNING *",
        uuid.uuid4(), body.task_id, uuid.UUID(user), acc.get("title", ""), when,
    )
    return _out(row)


@app.delete("/reminders/{reminder_id}", status_code=204)
async def delete_reminder(reminder_id: uuid.UUID, user: CurrentUser):
    done = await ctx.db.execute(
        "DELETE FROM reminders WHERE id = $1 AND user_id = $2", reminder_id, uuid.UUID(user)
    )
    if done.endswith(" 0"):
        raise HTTPException(404, "reminder not found")
