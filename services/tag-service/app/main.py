"""tag-service -- labels and categories attached to tasks.

Tags belong to the user who created them; assignments (task_tags) are
visible to everyone who can see the task. Assignment rights follow the task
(write access to its list), checked through task-service.
"""

import uuid

from fastapi import HTTPException, Query
from pydantic import BaseModel, Field

from taskflow_common import create_service
from taskflow_common.access import require_task, task_access
from taskflow_common.auth import CurrentUser

SCHEMA = """
CREATE TABLE IF NOT EXISTS tags (
    id         UUID PRIMARY KEY,
    owner_id   UUID NOT NULL,
    name       TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT '#4f46e5',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (owner_id, name)
);
CREATE TABLE IF NOT EXISTS task_tags (
    task_id   UUID NOT NULL,
    tag_id    UUID NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
    tagged_by UUID NOT NULL,
    PRIMARY KEY (task_id, tag_id)
);
"""


async def _on_task_deleted(subject: str, event: dict) -> None:
    await ctx.db.execute("DELETE FROM task_tags WHERE task_id = $1",
                         uuid.UUID(event["data"]["task"]["id"]))


async def _startup(c) -> None:
    c.bus.consume("tag-service-tasks", "taskflow.task.deleted", _on_task_deleted)


app, ctx = create_service("tag-service", schema=SCHEMA, use_nats=True, on_startup=_startup)


class NewTag(BaseModel):
    name: str = Field(min_length=1, max_length=30)
    color: str = Field(default="#4f46e5", pattern=r"^#[0-9a-fA-F]{6}$")


class Assignment(BaseModel):
    tag_ids: list[uuid.UUID] = Field(max_length=20)


def _out(row) -> dict:
    return {"id": str(row["id"]), "name": row["name"], "color": row["color"]}


async def _tags_of(task_ids: list[uuid.UUID]) -> dict[str, list[dict]]:
    rows = await ctx.db.fetch(
        "SELECT tt.task_id, t.id, t.name, t.color FROM task_tags tt "
        "JOIN tags t ON t.id = tt.tag_id WHERE tt.task_id = ANY($1::uuid[]) ORDER BY t.name",
        task_ids,
    )
    out: dict[str, list[dict]] = {str(t): [] for t in task_ids}
    for r in rows:
        out[str(r["task_id"])].append(_out(r))
    return out


@app.get("/tags")
async def my_tags(user: CurrentUser):
    rows = await ctx.db.fetch("SELECT * FROM tags WHERE owner_id = $1 ORDER BY name",
                              uuid.UUID(user))
    return [_out(r) for r in rows]


@app.post("/tags", status_code=201)
async def create_tag(body: NewTag, user: CurrentUser):
    row = await ctx.db.fetchrow(
        "INSERT INTO tags (id, owner_id, name, color) VALUES ($1, $2, $3, $4) "
        "ON CONFLICT (owner_id, name) DO UPDATE SET color = EXCLUDED.color RETURNING *",
        uuid.uuid4(), uuid.UUID(user), body.name.strip(), body.color,
    )
    return _out(row)


@app.delete("/tags/{tag_id}", status_code=204)
async def delete_tag(tag_id: uuid.UUID, user: CurrentUser):
    done = await ctx.db.execute("DELETE FROM tags WHERE id = $1 AND owner_id = $2",
                                tag_id, uuid.UUID(user))
    if done.endswith(" 0"):
        raise HTTPException(404, "tag not found")


@app.get("/tags/task/{task_id}")
async def task_tags(task_id: uuid.UUID, user: CurrentUser):
    await require_task(ctx, task_id, user)
    return (await _tags_of([task_id]))[str(task_id)]


@app.put("/tags/task/{task_id}")
async def set_task_tags(task_id: uuid.UUID, body: Assignment, user: CurrentUser):
    await require_task(ctx, task_id, user, write=True)
    async with ctx.db.acquire() as conn, conn.transaction():
        # Only the caller's own tags, or tags already on the task, may be set.
        allowed = {r["id"] for r in await conn.fetch(
            "SELECT id FROM tags WHERE id = ANY($1::uuid[]) AND (owner_id = $2 OR id IN "
            "(SELECT tag_id FROM task_tags WHERE task_id = $3))",
            body.tag_ids, uuid.UUID(user), task_id,
        )}
        await conn.execute("DELETE FROM task_tags WHERE task_id = $1", task_id)
        await conn.executemany(
            "INSERT INTO task_tags (task_id, tag_id, tagged_by) VALUES ($1, $2, $3)",
            [(task_id, t, uuid.UUID(user)) for t in body.tag_ids if t in allowed],
        )
    await ctx.bus.publish("task.tagged", {"task_id": str(task_id), "actor_id": user,
                                          "tag_ids": [str(t) for t in allowed]})
    return (await _tags_of([task_id]))[str(task_id)]


@app.get("/tags/tasks")
async def tags_for_tasks(user: CurrentUser, ids: str = Query(max_length=40 * 200)):
    """Tags of many tasks at once (the task table). Tasks the caller cannot
    see are silently left out."""
    wanted = []
    for raw in ids.split(",")[:200]:
        try:
            tid = uuid.UUID(raw)
        except ValueError:
            continue
        if await task_access(ctx, tid, user):
            wanted.append(tid)
    return await _tags_of(wanted) if wanted else {}
