"""The service factory every TaskFlow backend is built with.

    app, ctx = create_service("task-service", schema=SCHEMA, use_nats=True)

    @app.get("/tasks")
    async def list_tasks(user: CurrentUser): ...

``create_service`` wires logging, metrics, tracing, probes and the
registry, then connects only the dependencies the service asked for. The
connected clients live on ``ctx`` (``ctx.db``, ``ctx.bus``, ``ctx.redis``,
``ctx.http``).

Probes:
* ``/healthz`` (liveness) answers as long as the event loop runs. It checks
  no dependency, so a database outage never makes Kubernetes restart every
  service at once.
* ``/readyz`` (readiness) checks each connected dependency. A Pod that
  cannot reach its database is taken out of its Service's endpoints until
  it can.
"""

import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

import httpx
from fastapi import FastAPI
from fastapi.responses import JSONResponse

from . import config, db
from .events import EventBus
from .logging_setup import setup_logging
from .metrics import BUILD_INFO, MetricsMiddleware, metrics_endpoint
from .registry import Registry
from .tracing import setup_tracing

Hook = Callable[["ServiceContext"], Awaitable[None]]


@dataclass
class ServiceContext:
    log: logging.Logger
    db: Any = None
    bus: EventBus | None = None
    redis: Any = None
    http: httpx.AsyncClient | None = None
    registry: Registry | None = None
    checks: dict[str, Callable[[], Awaitable[None]]] = field(default_factory=dict)
    state: dict[str, Any] = field(default_factory=dict)

    async def call(
        self, service: str, method: str, path: str, user_id: str | None = None, **kwargs
    ) -> httpx.Response:
        """Synchronous service-to-service call (traced, identity forwarded)."""
        headers = kwargs.pop("headers", {})
        if user_id:
            headers["X-User-Id"] = user_id
        return await self.http.request(
            method, config.service_url(service) + path, headers=headers, **kwargs
        )


def create_service(
    name: str,
    *,
    schema: str | None = None,
    use_db: bool = True,
    use_nats: bool = False,
    use_redis: bool = False,
    on_startup: Hook | None = None,
    on_shutdown: Hook | None = None,
) -> tuple[FastAPI, ServiceContext]:
    config.SERVICE_NAME = config.env("SERVICE_NAME", name)
    log = setup_logging()
    ctx = ServiceContext(log=log)

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        ctx.http = httpx.AsyncClient(timeout=httpx.Timeout(5.0, connect=2.0))
        if use_db:
            ctx.db = await db.connect(schema)
            ctx.checks["postgres"] = lambda: db.check(ctx.db)
        if use_nats:
            ctx.bus = EventBus()
            await ctx.bus.connect()
            ctx.checks["nats"] = ctx.bus.check
        if use_redis:
            import redis.asyncio as aioredis

            ctx.redis = aioredis.from_url(
                config.env("REDIS_URL", "redis://redis:6379/0"), decode_responses=True
            )
            ctx.checks["redis"] = ctx.redis.ping
        if on_startup:
            await on_startup(ctx)
        ctx.registry = Registry()
        if config.env("REGISTRY_ENABLED", "true") == "true":
            await ctx.registry.start()
        BUILD_INFO.labels(config.SERVICE_NAME, config.SERVICE_VERSION).set(1)
        log.info("service started", extra={"fields": {"version": config.SERVICE_VERSION}})
        yield
        # Shutdown: leave the registry first so no new traffic is routed here.
        log.info("service stopping")
        await ctx.registry.stop()
        if on_shutdown:
            await on_shutdown(ctx)
        if ctx.bus:
            await ctx.bus.close()
        if ctx.redis:
            await ctx.redis.aclose()
        if ctx.db:
            await ctx.db.close()
        await ctx.http.aclose()

    app = FastAPI(title=name, version=config.SERVICE_VERSION, lifespan=lifespan)
    app.add_middleware(MetricsMiddleware)
    app.add_route("/metrics", metrics_endpoint, include_in_schema=False)
    setup_tracing(app)

    @app.get("/healthz", include_in_schema=False)
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz", include_in_schema=False)
    async def readyz():
        failed = {}
        for dep, check in ctx.checks.items():
            try:
                await check()
            except Exception as exc:  # noqa: BLE001 -- report every failing dependency
                failed[dep] = str(exc) or exc.__class__.__name__
        if failed:
            return JSONResponse({"status": "not ready", "failed": failed}, status_code=503)
        return {"status": "ready", "checks": list(ctx.checks)}

    return app, ctx
