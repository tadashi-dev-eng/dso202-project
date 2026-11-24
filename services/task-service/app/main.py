"""task-service -- core task CRUD: status, priority and due dates.

The busiest service and the one the request-rate HPA scales. Every change
is published as an event (``task.created``, ``task.updated``,
``task.completed``, ``task.deleted``); search, analytics, audit,
notification and recurrence react to those events without task-service
knowing they exist.

Access is decided by list-service (see taskflow_common.access).
"""

import uuid
from datetime import datetime
from typing import Literal

from fastapi import HTTPException, Query
from prometheus_client import Counter
from pydantic import BaseModel, Field

from taskflow_common import create_service
from taskflow_common.access import list_access, require_list
from taskflow_common.auth import CurrentUser

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id           UUID PRIMARY KEY,
    list_id      UUID NOT NULL,
    created_by   UUID NOT NULL,
    title        TEXT NOT NULL,
    description  TEXT NOT NULL DEFAULT '',
    status       TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo', 'in_progress', 'done')),
    priority     TEXT NOT NULL DEFAULT 'medium' CHECK (priority IN ('low', 'medium', 'high')),
    due_at       TIMESTAMPTZ,
    completed_at TIMESTAMPTZ,
    recurrence_of UUID,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS tasks_list ON tasks(list_id, created_at);
CREATE INDEX IF NOT EXISTS tasks_due ON tasks(due_at) WHERE status <> 'done';
"""

Status = Literal["todo", "in_progress", "done"]
Priority = Literal["low", "medium", "high"]

TASK_CHANGES = Counter("taskflow_task_changes_total", "Task changes", ["action"])


async def _on_list_deleted(subject: str, event: dict) -> None:
    list_id = uuid.UUID(event["data"]["list_id"])
    rows = await ctx.db.fetch("DELETE FROM tasks WHERE list_id = $1 RETURNING id", list_id)
    for r in rows:
        await ctx.bus.publish("task.deleted", {
            "task": {"id": str(r["id"]), "list_id": str(list_id)},
            "actor_id": event["data"].get("actor_id"), "reason": "list deleted",
        })
    ctx.log.info("removed tasks of deleted list",
                 extra={"fields": {"list_id": str(list_id), "count": len(rows)}})


async def _startup(c) -> None:
    c.bus.consume("task-service-lists", "taskflow.list.deleted", _on_list_deleted)


app, ctx = create_service("task-service", schema=SCHEMA, use_nats=True, on_startup=_startup)


class NewTask(BaseModel):
    list_id: uuid.UUID
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=5000)
    priority: Priority = "medium"
    status: Status = "todo"
    due_at: datetime | None = None
    recurrence_of: uuid.UUID | None = None


class TaskUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=5000)
    priority: Priority | None = None
    status: Status | None = None
    due_at: datetime | None = None
    clear_due: bool = False


def _out(row) -> dict:
    return {
        "id": str(row["id"]),
        "list_id": str(row["list_id"]),
        "created_by": str(row["created_by"]),
        "title": row["title"],
        "description": row["description"],
        "status": row["status"],
        "priority": row["priority"],
        "due_at": row["due_at"].isoformat() if row["due_at"] else None,
        "completed_at": row["completed_at"].isoformat() if row["completed_at"] else None,
        "recurrence_of": str(row["recurrence_of"]) if row["recurrence_of"] else None,
        "created_at": row["created_at"].isoformat(),
        "updated_at": row["updated_at"].isoformat(),
    }


async def _load(task_id: uuid.UUID):
    row = await ctx.db.fetchrow("SELECT * FROM tasks WHERE id = $1", task_id)
    if not row:
        raise HTTPException(404, "task not found")
    return row


async def _insert(body: NewTask, user: str) -> dict:
    row = await ctx.db.fetchrow(
        "INSERT INTO tasks (id, list_id, created_by, title, description, status, priority, "
        "due_at, recurrence_of, completed_at) VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9, "
        "CASE WHEN $6 = 'done' THEN now() END) RETURNING *",
        uuid.uuid4(), body.list_id, uuid.UUID(user), body.title, body.description,
        body.status, body.priority, body.due_at, body.recurrence_of,
    )
    task = _out(row)
    TASK_CHANGES.labels("created").inc()
    await ctx.bus.publish("task.created", {"task": task, "actor_id": user})
    return task


@app.get("/tasks")
async def list_tasks(
    user: CurrentUser,
    list_id: uuid.UUID | None = None,
    status: Status | None = None,
    limit: int = Query(default=200, le=500),
):
    if list_id:
        await require_list(ctx, list_id, user)
        list_ids = [list_id]
    else:
        resp = await ctx.call("list-service", "GET", "/lists", user_id=user)
        resp.raise_for_status()
        list_ids = [uuid.UUID(lst["id"]) for lst in resp.json()]
    rows = await ctx.db.fetch(
        "SELECT * FROM tasks WHERE list_id = ANY($1::uuid[]) "
        "AND ($2::text IS NULL OR status = $2) "
        "ORDER BY (status = 'done'), due_at NULLS LAST, created_at LIMIT $3",
        list_ids, status, limit,
    )
    return [_out(r) for r in rows]


@app.post("/tasks", status_code=201)
async def create_task(body: NewTask, user: CurrentUser):
    await require_list(ctx, body.list_id, user, write=True)
    return await _insert(body, user)


@app.get("/tasks/{task_id}")
async def get_task(task_id: uuid.UUID, user: CurrentUser):
    row = await _load(task_id)
    await require_list(ctx, row["list_id"], user)
    return _out(row)


@app.patch("/tasks/{task_id}")
async def update_task(task_id: uuid.UUID, body: TaskUpdate, user: CurrentUser):
    before = await _load(task_id)
    await require_list(ctx, before["list_id"], user, write=True)
    due = None if body.clear_due else (body.due_at or before["due_at"])
    row = await ctx.db.fetchrow(
        "UPDATE tasks SET title = COALESCE($2, title), description = COALESCE($3, description), "
        "priority = COALESCE($4, priority), status = COALESCE($5, status), due_at = $6, "
        "completed_at = CASE WHEN COALESCE($5, status) = 'done' "
        "                    THEN COALESCE(completed_at, now()) END, "
        "updated_at = now() WHERE id = $1 RETURNING *",
        task_id, body.title, body.description, body.priority, body.status, due,
    )
    task = _out(row)
    TASK_CHANGES.labels("updated").inc()
    await ctx.bus.publish("task.updated", {"task": task, "actor_id": user,
                                           "previous_status": before["status"]})
    if before["status"] != "done" and task["status"] == "done":
        TASK_CHANGES.labels("completed").inc()
        await ctx.bus.publish("task.completed", {"task": task, "actor_id": user})
    return task


@app.delete("/tasks/{task_id}", status_code=204)
async def delete_task(task_id: uuid.UUID, user: CurrentUser):
    row = await _load(task_id)
    await require_list(ctx, row["list_id"], user, write=True)
    await ctx.db.execute("DELETE FROM tasks WHERE id = $1", task_id)
    TASK_CHANGES.labels("deleted").inc()
    await ctx.bus.publish("task.deleted", {"task": _out(row), "actor_id": user})


# --- internal API ----------------------------------------------------------

@app.get("/internal/tasks/{task_id}/access")
async def task_access(task_id: uuid.UUID, user_id: uuid.UUID):
    """Map a task to its list and return the caller's role on that list."""
    row = await ctx.db.fetchrow("SELECT id, list_id, title FROM tasks WHERE id = $1", task_id)
    if not row:
        raise HTTPException(404, "task not found")
    acc = await list_access(ctx, row["list_id"], str(user_id))
    if acc is None:
        raise HTTPException(404, "no access")
    return {"task_id": str(task_id), "list_id": str(row["list_id"]), "title": row["title"],
            "role": acc["role"], "can_write": acc["can_write"]}


@app.get("/internal/tasks/{task_id}")
async def internal_get(task_id: uuid.UUID):
    return _out(await _load(task_id))


@app.post("/internal/tasks", status_code=201)
async def internal_create(body: NewTask, user: CurrentUser):
    """Used by recurrence-service to create the next occurrence on behalf of
    the user who completed the previous one."""
    return await _insert(body, user)
