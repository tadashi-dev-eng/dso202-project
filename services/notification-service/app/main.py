"""notification-service -- delivers notifications (simulated provider).

Reads every event through one durable JetStream consumer,
``notification-service``, shared by all replicas: JetStream hands each
message to exactly one replica, so adding replicas adds throughput.

Delivery goes through a *simulated* e-mail/push provider that takes
``PROVIDER_LATENCY_MS`` per message (a real provider's API call). That makes
throughput per replica bounded and realistic: under load the consumer's
backlog grows, and the HPA scales this Deployment on that backlog
(``taskflow_notification_backlog``, exported here and exposed to the HPA
through Prometheus Adapter as an external metric).

In-app notifications are kept per user in Redis (latest 50), which is all
the UI's bell needs.
"""

import asyncio
import json
import time
import uuid

from prometheus_client import Counter, Gauge, Histogram

from taskflow_common import create_service
from taskflow_common.auth import CurrentUser
from taskflow_common.config import env_int
from taskflow_common.users import display_name, profile

DURABLE = "notification-service"
LATENCY = env_int("PROVIDER_LATENCY_MS", 200) / 1000
KEEP = 50

SENT = Counter("taskflow_notifications_sent_total", "Notifications delivered", ["channel", "type"])
BACKLOG = Gauge("taskflow_notification_backlog",
                "Events waiting in the notification consumer (pending + unacked)")
DELIVERY = Histogram("taskflow_notification_delivery_seconds", "Provider call latency",
                     buckets=(0.05, 0.1, 0.2, 0.3, 0.5, 1, 2))


async def _members(list_id: str) -> list[str]:
    resp = await ctx.call("list-service", "GET", f"/internal/lists/{list_id}/members")
    return [m["user_id"] for m in resp.json()] if resp.status_code == 200 else []


async def _deliver(user_id: str, ntype: str, title: str, body: str, link: dict) -> None:
    prefs = (await profile(ctx, user_id) or {}).get("preferences", {})
    channels = ["in-app"]
    if prefs.get("email_notifications", True):
        channels.append("email")
    if prefs.get("push_notifications", True):
        channels.append("push")
    # The simulated provider: one outbound call per notification.
    start = time.perf_counter()
    await asyncio.sleep(LATENCY)
    DELIVERY.observe(time.perf_counter() - start)
    note = {
        "id": str(uuid.uuid4()), "type": ntype, "title": title, "body": body,
        "channel": ", ".join(channels), "created_at": time.time(), **link,
    }
    pipe = ctx.redis.pipeline()
    pipe.lpush(f"notif:{user_id}", json.dumps(note))
    pipe.ltrim(f"notif:{user_id}", 0, KEEP - 1)
    await pipe.execute()
    for ch in channels:
        SENT.labels(ch, ntype).inc()
    ctx.log.info("notification delivered",
                 extra={"fields": {"user_id": user_id, "type": ntype, "channels": channels}})


async def _handle(subject: str, event: dict) -> None:
    data = event.get("data", {})
    actor = data.get("actor_id")
    if subject == "reminder.due":
        await _deliver(data["user_id"], "reminder", f"Reminder: {data['task_title']}",
                       f"Due {data['remind_at'][:16].replace('T', ' ')} UTC",
                       {"task_id": data["task_id"]})
    elif subject == "comment.created":
        who = data["comment"].get("author_name") or "Someone"
        for uid in await _members(data["list_id"]):
            if uid != actor:
                await _deliver(uid, "comment", f"{who} commented on “{data.get('task_title')}”",
                               data["comment"]["body"][:200], {"task_id": data["task_id"]})
    elif subject == "task.completed":
        task = data["task"]
        who = await display_name(ctx, actor) or "Someone"
        for uid in await _members(task["list_id"]):
            if uid != actor:
                await _deliver(uid, "completed", f"{who} completed “{task['title']}”", "",
                               {"task_id": task["id"]})
    elif subject == "list.shared":
        who = await display_name(ctx, actor) or "Someone"
        await _deliver(data["user_id"], "shared", f"{who} shared a list with you",
                       f"You can now {'edit' if data['role'] == 'editor' else 'view'} it.",
                       {"list_id": data["list_id"]})
    # Every other event type needs no notification and is acknowledged at once.


async def _backlog_loop(c) -> None:
    while True:
        try:
            BACKLOG.set(await c.bus.pending(DURABLE))
        except Exception:  # noqa: BLE001 -- consumer may not exist yet
            pass
        await asyncio.sleep(5)


async def _startup(c) -> None:
    c.bus.consume(DURABLE, "taskflow.>", _handle, batch=20)
    c.state["backlog"] = asyncio.create_task(_backlog_loop(c))


async def _shutdown(c) -> None:
    c.state["backlog"].cancel()


app, ctx = create_service("notification-service", use_db=False, use_nats=True, use_redis=True,
                          on_startup=_startup, on_shutdown=_shutdown)


@app.get("/notifications")
async def my_notifications(user: CurrentUser):
    raw = await ctx.redis.lrange(f"notif:{user}", 0, KEEP - 1)
    read_at = float(await ctx.redis.get(f"notif-read:{user}") or 0)
    out = []
    for item in raw:
        n = json.loads(item)
        n["read"] = n["created_at"] <= read_at
        n["created_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(n["created_at"]))
        out.append(n)
    return out


@app.post("/notifications/read-all", status_code=204)
async def read_all(user: CurrentUser):
    await ctx.redis.set(f"notif-read:{user}", time.time())
