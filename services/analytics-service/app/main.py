"""analytics-service -- productivity statistics and business metrics.

Consumes task and comment events and keeps counters in Redis:

    stats:<user>              hash   created / completed / comments
    stats:<user>:<YYYY-MM-DD> hash   created / completed that day (kept 35 days)
    stats:global              hash   platform totals
    active-users              zset   user -> last activity (unix time)

Two outputs:
* ``GET /stats/me`` feeds the UI's statistics page;
* ``/metrics`` exports business metrics for the application dashboard:
  ``taskflow_business_events_total`` (counter, by event) and gauges for
  platform totals and users active in the last 15 minutes. The gauges are
  read from Redis, so every replica reports the same value; dashboards use
  ``max()`` across replicas.
"""

import asyncio
import time
from datetime import datetime, timedelta, timezone

from prometheus_client import Counter, Gauge

from taskflow_common import create_service
from taskflow_common.auth import CurrentUser

EVENTS = Counter("taskflow_business_events_total", "Business events observed", ["event"])
TOTALS = Gauge("taskflow_platform_total", "Platform-wide totals", ["kind"])
ACTIVE = Gauge("taskflow_active_users", "Users active in the last 15 minutes")
DAY_TTL = 35 * 24 * 3600


def _day(ts: float | None = None) -> str:
    return datetime.fromtimestamp(ts or time.time(), timezone.utc).strftime("%Y-%m-%d")


async def _bump(user: str | None, field: str, ts: float) -> None:
    pipe = ctx.redis.pipeline()
    pipe.hincrby("stats:global", field, 1)
    if user:
        pipe.hincrby(f"stats:{user}", field, 1)
        key = f"stats:{user}:{_day(ts)}"
        pipe.hincrby(key, field, 1)
        pipe.expire(key, DAY_TTL)
        pipe.zadd("active-users", {user: ts})
    await pipe.execute()


async def _handle(subject: str, event: dict) -> None:
    data = event.get("data", {})
    ts = event.get("time") or time.time()
    actor = data.get("actor_id")
    EVENTS.labels(subject).inc()
    if subject == "task.created":
        await _bump(actor, "created", ts)
    elif subject == "task.completed":
        await _bump(actor, "completed", ts)
    elif subject == "comment.created":
        await _bump(actor, "comments", ts)
    elif subject == "task.deleted":
        await ctx.redis.hincrby("stats:global", "deleted", 1)
    elif actor:
        await ctx.redis.zadd("active-users", {actor: ts})


async def _gauges(c) -> None:
    while True:
        try:
            totals = await c.redis.hgetall("stats:global")
            for kind in ("created", "completed", "comments", "deleted"):
                TOTALS.labels(kind).set(int(totals.get(kind, 0)))
            cutoff = time.time() - 15 * 60
            await c.redis.zremrangebyscore("active-users", 0, time.time() - 30 * 24 * 3600)
            ACTIVE.set(await c.redis.zcount("active-users", cutoff, "+inf"))
        except Exception:  # noqa: BLE001
            pass
        await asyncio.sleep(10)


async def _startup(c) -> None:
    c.bus.consume("analytics-service-tasks", "taskflow.task.>", _handle)
    c.bus.consume("analytics-service-comments", "taskflow.comment.>", _handle)
    c.bus.consume("analytics-service-lists", "taskflow.list.>", _handle)
    c.state["gauges"] = asyncio.create_task(_gauges(c))


async def _shutdown(c) -> None:
    c.state["gauges"].cancel()


app, ctx = create_service("analytics-service", use_db=False, use_nats=True, use_redis=True,
                          on_startup=_startup, on_shutdown=_shutdown)


@app.get("/stats/me")
async def my_stats(user: CurrentUser):
    totals = await ctx.redis.hgetall(f"stats:{user}")
    created = int(totals.get("created", 0))
    completed = int(totals.get("completed", 0))
    today = datetime.now(timezone.utc).date()
    days = [(today - timedelta(days=i)).isoformat() for i in range(6, -1, -1)]
    pipe = ctx.redis.pipeline()
    for d in days:
        pipe.hgetall(f"stats:{user}:{d}")
    per_day = await pipe.execute()
    return {
        "created": created,
        "completed": completed,
        "comments": int(totals.get("comments", 0)),
        "completion_rate": round(100 * completed / created) if created else 0,
        "last_7_days": [
            {"day": d, "created": int(v.get("created", 0)), "completed": int(v.get("completed", 0))}
            for d, v in zip(days, per_day)
        ],
    }
