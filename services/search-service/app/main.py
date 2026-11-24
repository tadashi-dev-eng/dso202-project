"""search-service -- full-text search across tasks and comments.

The index is built only from events (task.*, comment.*): search-service
never queries another service's database. It is a PostgreSQL table with a
generated ``tsvector`` column and a GIN index, so the index survives
restarts on the database's PersistentVolume and needs no extra search
engine.

Results are filtered to the lists the caller belongs to (asked of
list-service per query). Matches are marked with « » in plain text, which
the frontend renders as highlights without interpreting any HTML.
"""

import uuid

from fastapi import Query
from prometheus_client import Counter, Gauge

from taskflow_common import create_service
from taskflow_common.auth import CurrentUser

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    kind       TEXT NOT NULL CHECK (kind IN ('task', 'comment')),
    id         UUID NOT NULL,
    task_id    UUID NOT NULL,
    list_id    UUID NOT NULL,
    title      TEXT NOT NULL DEFAULT '',
    body       TEXT NOT NULL DEFAULT '',
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    tsv        tsvector GENERATED ALWAYS AS (
                   setweight(to_tsvector('english', title), 'A') ||
                   setweight(to_tsvector('english', body), 'B')) STORED,
    PRIMARY KEY (kind, id)
);
CREATE INDEX IF NOT EXISTS documents_tsv ON documents USING GIN (tsv);
CREATE INDEX IF NOT EXISTS documents_task ON documents(task_id);
"""

INDEXED = Counter("taskflow_search_index_updates_total", "Index changes", ["kind", "op"])
DOCS = Gauge("taskflow_search_documents", "Documents in the search index")


async def _upsert(kind, doc_id, task_id, list_id, title, body):
    await ctx.db.execute(
        "INSERT INTO documents (kind, id, task_id, list_id, title, body) "
        "VALUES ($1, $2, $3, $4, $5, $6) ON CONFLICT (kind, id) DO UPDATE SET "
        "title = EXCLUDED.title, body = EXCLUDED.body, list_id = EXCLUDED.list_id, "
        "updated_at = now()",
        kind, uuid.UUID(doc_id), uuid.UUID(task_id), uuid.UUID(list_id), title, body,
    )
    INDEXED.labels(kind, "upsert").inc()


async def _handle(subject: str, event: dict) -> None:
    data = event["data"]
    if subject in ("task.created", "task.updated"):
        t = data["task"]
        await _upsert("task", t["id"], t["id"], t["list_id"], t["title"], t.get("description", ""))
    elif subject == "task.deleted":
        await ctx.db.execute("DELETE FROM documents WHERE task_id = $1",
                             uuid.UUID(data["task"]["id"]))
        INDEXED.labels("task", "delete").inc()
    elif subject == "comment.created":
        c = data["comment"]
        await _upsert("comment", c["id"], data["task_id"], data["list_id"],
                      data.get("task_title") or "", c["body"])
    elif subject == "comment.deleted":
        await ctx.db.execute("DELETE FROM documents WHERE kind = 'comment' AND id = $1",
                             uuid.UUID(data["comment_id"]))
        INDEXED.labels("comment", "delete").inc()
    DOCS.set(await ctx.db.fetchval("SELECT count(*) FROM documents"))


async def _startup(c) -> None:
    c.bus.consume("search-service-tasks", "taskflow.task.>", _handle)
    c.bus.consume("search-service-comments", "taskflow.comment.>", _handle)


app, ctx = create_service("search-service", schema=SCHEMA, use_nats=True, on_startup=_startup)


@app.get("/search")
async def search(user: CurrentUser, q: str = Query(min_length=1, max_length=200),
                 limit: int = Query(default=20, le=50)):
    resp = await ctx.call("list-service", "GET", "/lists", user_id=user)
    resp.raise_for_status()
    list_ids = [uuid.UUID(lst["id"]) for lst in resp.json()]
    rows = await ctx.db.fetch(
        "SELECT kind, id, task_id, list_id, title, "
        "ts_headline('english', CASE WHEN kind = 'task' THEN title || ' — ' || body ELSE body END, "
        "  query, 'StartSel=«, StopSel=», MaxWords=18, MinWords=6') AS snippet, "
        "ts_rank(tsv, query) AS rank "
        "FROM documents, websearch_to_tsquery('english', $1) query "
        "WHERE tsv @@ query AND list_id = ANY($2::uuid[]) "
        "ORDER BY rank DESC, updated_at DESC LIMIT $3",
        q, list_ids, limit,
    )
    return [
        {"kind": r["kind"], "id": str(r["id"]), "task_id": str(r["task_id"]),
         "list_id": str(r["list_id"]), "title": r["title"],
         "snippet": r["snippet"].replace("<", "‹").replace(">", "›")}
        for r in rows
    ]
