"""attachment-service -- file upload and download for tasks.

File bytes go to S3-compatible object storage (bucket ``attachments`` on
the in-cluster object store, backed by a PersistentVolume); only metadata
(name, type, size, object key) is kept in PostgreSQL. Downloads stream
from the object store through this service, so the store itself is never
exposed outside the cluster and access is checked on every download.

The S3 client is synchronous, so its calls run in worker threads.
"""

import asyncio
import uuid

from fastapi import HTTPException, Request, UploadFile
from fastapi.responses import StreamingResponse
from minio import Minio
from prometheus_client import Counter

from taskflow_common import create_service
from taskflow_common.access import require_task
from taskflow_common.auth import CurrentUser
from taskflow_common.config import env, env_int

SCHEMA = """
CREATE TABLE IF NOT EXISTS attachments (
    id           UUID PRIMARY KEY,
    task_id      UUID NOT NULL,
    uploader_id  UUID NOT NULL,
    filename     TEXT NOT NULL,
    content_type TEXT NOT NULL,
    size         BIGINT NOT NULL,
    object_key   TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS attachments_task ON attachments(task_id);
"""

BUCKET = env("S3_BUCKET", "attachments")
MAX_BYTES = env_int("MAX_UPLOAD_BYTES", 10 * 1024 * 1024)
BYTES = Counter("taskflow_attachment_bytes_total", "Bytes moved", ["direction"])

s3 = Minio(
    env("S3_ENDPOINT", "objectstore:8333"),
    access_key=env("S3_ACCESS_KEY", "taskflow"),
    secret_key=env("S3_SECRET_KEY", "dev-secret-key"),
    secure=False,
    region="us-east-1",
)


async def _bucket_check() -> None:
    if not await asyncio.to_thread(s3.bucket_exists, BUCKET):
        raise RuntimeError(f"bucket {BUCKET} missing")


async def _on_task_deleted(subject: str, event: dict) -> None:
    rows = await ctx.db.fetch("DELETE FROM attachments WHERE task_id = $1 RETURNING object_key",
                              uuid.UUID(event["data"]["task"]["id"]))
    for r in rows:
        await asyncio.to_thread(s3.remove_object, BUCKET, r["object_key"])


async def _startup(c) -> None:
    for attempt in range(30):
        try:
            if not await asyncio.to_thread(s3.bucket_exists, BUCKET):
                await asyncio.to_thread(s3.make_bucket, BUCKET)
            break
        except Exception as exc:  # noqa: BLE001 -- object store may still be starting
            c.log.warning("object store not ready",
                          extra={"fields": {"attempt": attempt, "error": str(exc)}})
            await asyncio.sleep(2)
    c.checks["objectstore"] = _bucket_check
    c.bus.consume("attachment-service-tasks", "taskflow.task.deleted", _on_task_deleted)


app, ctx = create_service("attachment-service", schema=SCHEMA, use_nats=True,
                          on_startup=_startup)


def _out(row) -> dict:
    return {
        "id": str(row["id"]),
        "task_id": str(row["task_id"]),
        "filename": row["filename"],
        "content_type": row["content_type"],
        "size": row["size"],
        "created_at": row["created_at"].isoformat(),
    }


@app.get("/attachments")
async def list_attachments(task_id: uuid.UUID, user: CurrentUser):
    await require_task(ctx, task_id, user)
    rows = await ctx.db.fetch(
        "SELECT * FROM attachments WHERE task_id = $1 ORDER BY created_at", task_id
    )
    return [_out(r) for r in rows]


@app.post("/attachments", status_code=201)
async def upload(task_id: uuid.UUID, file: UploadFile, request: Request, user: CurrentUser):
    await require_task(ctx, task_id, user, write=True)
    data = await file.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise HTTPException(413, f"file larger than {MAX_BYTES // (1024 * 1024)} MB")
    att_id = uuid.uuid4()
    key = f"{task_id}/{att_id}"
    content_type = file.content_type or "application/octet-stream"

    import io

    await asyncio.to_thread(s3.put_object, BUCKET, key, io.BytesIO(data), len(data),
                            content_type=content_type)
    BYTES.labels("in").inc(len(data))
    row = await ctx.db.fetchrow(
        "INSERT INTO attachments (id, task_id, uploader_id, filename, content_type, size, "
        "object_key) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING *",
        att_id, task_id, uuid.UUID(user), (file.filename or "file")[:200], content_type,
        len(data), key,
    )
    out = _out(row)
    await ctx.bus.publish("attachment.added", {"attachment": out, "actor_id": user})
    return out


@app.get("/attachments/{att_id}/download")
async def download(att_id: uuid.UUID, user: CurrentUser):
    row = await ctx.db.fetchrow("SELECT * FROM attachments WHERE id = $1", att_id)
    if not row:
        raise HTTPException(404, "attachment not found")
    await require_task(ctx, row["task_id"], user)
    obj = await asyncio.to_thread(s3.get_object, BUCKET, row["object_key"])

    def chunks():
        try:
            for chunk in obj.stream(64 * 1024):
                BYTES.labels("out").inc(len(chunk))
                yield chunk
        finally:
            obj.close()
            obj.release_conn()

    safe_name = row["filename"].replace('"', "")
    return StreamingResponse(
        chunks(), media_type=row["content_type"],
        headers={"Content-Disposition": f'attachment; filename="{safe_name}"',
                 "Content-Length": str(row["size"])},
    )


@app.delete("/attachments/{att_id}", status_code=204)
async def delete(att_id: uuid.UUID, user: CurrentUser):
    row = await ctx.db.fetchrow("SELECT * FROM attachments WHERE id = $1", att_id)
    if not row:
        raise HTTPException(404, "attachment not found")
    await require_task(ctx, row["task_id"], user, write=True)
    await asyncio.to_thread(s3.remove_object, BUCKET, row["object_key"])
    await ctx.db.execute("DELETE FROM attachments WHERE id = $1", att_id)
    await ctx.bus.publish("attachment.deleted", {
        "attachment_id": str(att_id), "task_id": str(row["task_id"]), "actor_id": user,
    })
