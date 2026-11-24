"""comment-service -- comments and activity notes on tasks.

Anyone who can see a task may comment on it. A new comment is published as
``comment.created`` with the task's list, which drives notifications to the
other list members, the search index and the audit log.
"""

import uuid

from fastapi import HTTPException
from pydantic import BaseModel, Field

from taskflow_common import create_service
from taskflow_common.access import require_task
from taskflow_common.auth import CurrentUser
from taskflow_common.users import display_name

SCHEMA = """
CREATE TABLE IF NOT EXISTS comments (
    id          UUID PRIMARY KEY,
    task_id     UUID NOT NULL,
    list_id     UUID NOT NULL,
    author_id   UUID NOT NULL,
    author_name TEXT,
    body        TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS comments_task ON comments(task_id, created_at);
"""


async def _on_task_deleted(subject: str, event: dict) -> None:
    await ctx.db.execute("DELETE FROM comments WHERE task_id = $1",
                         uuid.UUID(event["data"]["task"]["id"]))


async def _startup(c) -> None:
    c.bus.consume("comment-service-tasks", "taskflow.task.deleted", _on_task_deleted)


app, ctx = create_service("comment-service", schema=SCHEMA, use_nats=True, on_startup=_startup)


class NewComment(BaseModel):
    task_id: uuid.UUID
    body: str = Field(min_length=1, max_length=4000)


def _out(row) -> dict:
    return {
        "id": str(row["id"]),
        "task_id": str(row["task_id"]),
        "author_id": str(row["author_id"]),
        "author_name": row["author_name"],
        "body": row["body"],
        "created_at": row["created_at"].isoformat(),
    }


@app.get("/comments")
async def list_comments(task_id: uuid.UUID, user: CurrentUser):
    await require_task(ctx, task_id, user)
    rows = await ctx.db.fetch(
        "SELECT * FROM comments WHERE task_id = $1 ORDER BY created_at", task_id
    )
    return [_out(r) for r in rows]


@app.post("/comments", status_code=201)
async def add_comment(body: NewComment, user: CurrentUser):
    acc = await require_task(ctx, body.task_id, user)
    name = await display_name(ctx, user)
    row = await ctx.db.fetchrow(
        "INSERT INTO comments (id, task_id, list_id, author_id, author_name, body) "
        "VALUES ($1, $2, $3, $4, $5, $6) RETURNING *",
        uuid.uuid4(), body.task_id, uuid.UUID(acc["list_id"]), uuid.UUID(user), name,
        body.body.strip(),
    )
    comment = _out(row)
    await ctx.bus.publish("comment.created", {
        "comment": comment, "task_id": str(body.task_id), "list_id": acc["list_id"],
        "task_title": acc.get("title"), "actor_id": user,
    })
    return comment


@app.delete("/comments/{comment_id}", status_code=204)
async def delete_comment(comment_id: uuid.UUID, user: CurrentUser):
    row = await ctx.db.fetchrow(
        "DELETE FROM comments WHERE id = $1 AND author_id = $2 RETURNING task_id, list_id",
        comment_id, uuid.UUID(user),
    )
    if not row:
        raise HTTPException(404, "comment not found")
    await ctx.bus.publish("comment.deleted", {
        "comment_id": str(comment_id), "task_id": str(row["task_id"]),
        "list_id": str(row["list_id"]), "actor_id": user,
    })
