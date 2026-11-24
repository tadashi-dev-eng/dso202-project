"""api-gateway -- the single entry point for the API.

    browser --> Ingress (/api) --> api-gateway --> <service>

For every request the gateway:

1. **Routes** ``/api/<resource>/...`` to the owning service and strips the
   ``/api`` prefix (``/api/tasks/7`` -> task-service ``/tasks/7``).
2. **Authenticates**: verifies the JWT access token (except under
   ``/api/auth``), then replaces any client-sent ``X-User-Id`` with the
   token's subject.
3. **Rate-limits** per user (or per client IP before login) with a Redis
   fixed-window counter. The limit is read from etcd
   (``/taskflow/config/api-gateway/rate_limit_per_minute``) every 10 s, so
   it can be changed at runtime with ``etcdctl put`` and no redeploy.
4. **Discovers** a live instance through the etcd service registry and
   falls back to the Kubernetes Service DNS name if the registry is empty
   or unreachable, or the chosen instance refuses the connection.
"""

import random
import time

import httpx
import jwt
from fastapi import Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import Counter, Gauge

from taskflow_common import create_service
from taskflow_common.config import env, env_int, service_url

ROUTES = {
    "auth": "auth-service",
    "users": "user-service",
    "lists": "list-service",
    "tasks": "task-service",
    "tags": "tag-service",
    "comments": "comment-service",
    "attachments": "attachment-service",
    "reminders": "reminder-service",
    "recurrences": "recurrence-service",
    "notifications": "notification-service",
    "search": "search-service",
    "stats": "analytics-service",
    "audit": "audit-service",
}
PUBLIC = {"auth"}  # no token needed (register, login, refresh)

JWT_SECRET = env("JWT_SECRET", "dev-only-secret")
DEFAULT_LIMIT = env_int("RATE_LIMIT_PER_MINUTE", 600)

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
    "trailers", "transfer-encoding", "upgrade", "host", "content-length",
}

UPSTREAM = Counter("gateway_upstream_requests_total", "Requests proxied",
                   ["upstream", "discovery", "status"])
RATE_LIMITED = Counter("gateway_rate_limited_total", "Requests rejected by the rate limit")
AUTH_FAILED = Counter("gateway_auth_failures_total", "Requests with a missing or bad token",
                      ["reason"])
LIMIT_GAUGE = Gauge("gateway_rate_limit_per_minute", "Rate limit currently in force")

BREAKER_OPEN = Counter("gateway_circuit_open_total",
                       "Requests short-circuited because the upstream is failing", ["upstream"])

_limit = {"value": DEFAULT_LIMIT, "checked": 0.0}
_instances: dict[str, tuple[float, list[dict]]] = {}
# Circuit breaker: upstream -> monotonic time until which it is skipped.
# An unreachable service then costs every caller nothing instead of a
# connect timeout each, so one failing service cannot tie up the gateway.
BREAKER_SECONDS = env_int("CIRCUIT_BREAKER_SECONDS", 10)
_broken: dict[str, float] = {}


async def _startup(c) -> None:
    # One upstream client with connection pooling and a longer read timeout
    # (attachments can be large).
    c.state["upstream"] = httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=2.0))
    LIMIT_GAUGE.set(DEFAULT_LIMIT)


async def _shutdown(c) -> None:
    await c.state["upstream"].aclose()


app, ctx = create_service("api-gateway", use_db=False, use_redis=True,
                          on_startup=_startup, on_shutdown=_shutdown)


async def _rate_limit() -> int:
    """Current limit, refreshed from etcd at most every 10 seconds."""
    now = time.monotonic()
    if now - _limit["checked"] > 10:
        _limit["checked"] = now
        try:
            value = await ctx.registry.get_config("api-gateway", "rate_limit_per_minute")
            _limit["value"] = int(value) if value else DEFAULT_LIMIT
        except Exception:  # noqa: BLE001 -- keep the last known value
            pass
        LIMIT_GAUGE.set(_limit["value"])
    return _limit["value"]


async def _resolve(service: str) -> tuple[str, str]:
    """(base_url, how it was found)."""
    now = time.monotonic()
    cached = _instances.get(service)
    if not cached or cached[0] < now:
        try:
            found = await ctx.registry.instances(service)
        except Exception:  # noqa: BLE001 -- registry down: DNS still works
            found = []
        _instances[service] = (now + 5, found)
        cached = _instances[service]
    if cached[1]:
        return random.choice(cached[1])["url"], "registry"
    return service_url(service), "dns"


def _client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


@app.api_route("/api/{resource}/{path:path}",
               methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
@app.api_route("/api/{resource}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
async def proxy(request: Request, resource: str, path: str = ""):
    service = ROUTES.get(resource)
    if service is None:
        return JSONResponse({"detail": f"unknown API resource '{resource}'"}, status_code=404)

    # 2. authentication
    user_id = None
    if resource not in PUBLIC:
        header = request.headers.get("authorization", "")
        if not header.lower().startswith("bearer "):
            AUTH_FAILED.labels("missing").inc()
            return JSONResponse({"detail": "missing bearer token"}, status_code=401)
        try:
            claims = jwt.decode(header[7:], JWT_SECRET, algorithms=["HS256"], issuer="taskflow")
            user_id = claims["sub"]
        except jwt.ExpiredSignatureError:
            AUTH_FAILED.labels("expired").inc()
            return JSONResponse({"detail": "token expired"}, status_code=401)
        except jwt.PyJWTError:
            AUTH_FAILED.labels("invalid").inc()
            return JSONResponse({"detail": "invalid token"}, status_code=401)

    # 3. rate limit (fixed one-minute window)
    limit = await _rate_limit()
    window = int(time.time() // 60)
    key = f"rl:{user_id or 'ip:' + _client_ip(request)}:{window}"
    count = await ctx.redis.incr(key)
    if count == 1:
        await ctx.redis.expire(key, 70)
    if count > limit:
        RATE_LIMITED.inc()
        return JSONResponse({"detail": "rate limit exceeded"}, status_code=429,
                            headers={"Retry-After": str(60 - int(time.time()) % 60)})

    # 1 + 4. route to a discovered instance (unless the breaker is open)
    if _broken.get(service, 0) > time.monotonic():
        BREAKER_OPEN.labels(service).inc()
        return JSONResponse({"detail": f"{service} unavailable (circuit open)"}, status_code=503,
                            headers={"Retry-After": str(BREAKER_SECONDS)})
    headers ={k: v for k, v in request.headers.items()
               if k.lower() not in HOP_BY_HOP and k.lower() != "x-user-id"}
    if user_id:
        headers["X-User-Id"] = user_id
    headers["X-Forwarded-For"] = _client_ip(request)
    target_path = f"/{resource}/{path}" if path else f"/{resource}"
    body = await request.body()
    client: httpx.AsyncClient = ctx.state["upstream"]

    base, how = await _resolve(service)
    req = client.build_request(request.method, base + target_path, params=request.query_params,
                               headers=headers, content=body)
    try:
        upstream = await client.send(req, stream=True)
    except (httpx.ConnectError, httpx.ConnectTimeout):
        # Stale registry entry (Pod gone, lease not yet expired): use DNS.
        _instances.pop(service, None)
        how = "dns-fallback"
        req = client.build_request(request.method, service_url(service) + target_path,
                                   params=request.query_params, headers=headers, content=body)
        try:
            upstream = await client.send(req, stream=True)
        except httpx.HTTPError:
            _broken[service] = time.monotonic() + BREAKER_SECONDS
            ctx.log.warning("upstream unreachable, circuit opened",
                            extra={"fields": {"upstream": service}})
            UPSTREAM.labels(service, how, "502").inc()
            return JSONResponse({"detail": f"{service} unavailable"}, status_code=502)
    except httpx.TimeoutException:
        UPSTREAM.labels(service, how, "504").inc()
        return JSONResponse({"detail": f"{service} timed out"}, status_code=504)

    UPSTREAM.labels(service, how, str(upstream.status_code)).inc()
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() not in HOP_BY_HOP}
    if upstream.status_code == 204:
        await upstream.aclose()
        return Response(status_code=204, headers=out_headers)
    return StreamingResponse(upstream.aiter_raw(), status_code=upstream.status_code,
                             headers=out_headers, background=_close(upstream))


def _close(resp: httpx.Response):
    from starlette.background import BackgroundTask

    return BackgroundTask(resp.aclose)


@app.get("/api")
async def index():
    return {"service": "taskflow api-gateway", "resources": sorted(ROUTES)}
