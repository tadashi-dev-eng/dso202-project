"""Service registry and dynamic configuration on etcd.

Registration: on startup each Pod writes
``/taskflow/services/<service>/<pod>`` = ``{"url": ..., "version": ...}``
attached to a 15 s *lease* and keeps the lease alive every 5 s. If the Pod
dies without deregistering, the lease expires and etcd deletes the key on
its own, so the registry never lists a dead instance for long. The gateway
reads the prefix to find live instances.

Dynamic configuration: ``/taskflow/config/<service>/<key>`` holds values
that can change at runtime without a redeploy (e.g. the gateway's rate
limit). ``get_config`` reads them.

The etcd v3 JSON gateway (``/v3/...``) is used through httpx, so no gRPC
client library is needed. Keys and values are base64 in that API.
"""

import asyncio
import base64
import json
import logging
import time

import httpx

from . import config

log = logging.getLogger(__name__)

SERVICES_PREFIX = "/taskflow/services/"
CONFIG_PREFIX = "/taskflow/config/"
LEASE_TTL = 15


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


def _unb64(text: str) -> str:
    return base64.b64decode(text).decode()


def _prefix_end(prefix: str) -> str:
    """etcd range_end for 'every key starting with prefix'."""
    return prefix[:-1] + chr(ord(prefix[-1]) + 1)


class Registry:
    def __init__(self) -> None:
        self.endpoint = config.env("ETCD_ENDPOINT", "http://etcd:2379")
        self.client = httpx.AsyncClient(base_url=self.endpoint, timeout=3)
        self.lease_id: str | None = None
        self.key = f"{SERVICES_PREFIX}{config.SERVICE_NAME}/{config.POD_NAME}"
        self._task: asyncio.Task | None = None

    async def _post(self, path: str, body: dict) -> dict:
        resp = await self.client.post(path, json=body)
        resp.raise_for_status()
        return resp.json()

    async def register(self) -> None:
        value = json.dumps(
            {
                "service": config.SERVICE_NAME,
                "pod": config.POD_NAME,
                "url": f"http://{config.POD_IP}:{config.PORT}",
                "version": config.SERVICE_VERSION,
                "registered_at": time.time(),
            }
        )
        lease = await self._post("/v3/lease/grant", {"TTL": LEASE_TTL})
        self.lease_id = lease["ID"]
        await self._post(
            "/v3/kv/put",
            {"key": _b64(self.key), "value": _b64(value), "lease": self.lease_id},
        )
        log.info("registered in etcd", extra={"fields": {"key": self.key}})

    async def _keepalive(self) -> None:
        while True:
            await asyncio.sleep(LEASE_TTL / 3)
            try:
                if self.lease_id is None:
                    await self.register()
                    continue
                result = await self._post("/v3/lease/keepalive", {"ID": self.lease_id})
                # TTL absent or 0 means the lease already expired: register again.
                if int(result.get("result", {}).get("TTL", 0)) <= 0:
                    self.lease_id = None
            except Exception as exc:  # noqa: BLE001 -- etcd outage must not kill the service
                log.warning("etcd keepalive failed", extra={"fields": {"error": str(exc)}})
                self.lease_id = None

    async def start(self) -> None:
        try:
            await self.register()
        except Exception as exc:  # noqa: BLE001 -- the keepalive loop retries
            log.warning("etcd registration deferred", extra={"fields": {"error": str(exc)}})
        self._task = asyncio.create_task(self._keepalive())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
        if self.lease_id:
            # Revoking the lease deletes the key at once (graceful shutdown).
            try:
                await self._post("/v3/lease/revoke", {"ID": self.lease_id})
            except Exception:  # noqa: BLE001
                pass
        await self.client.aclose()

    async def instances(self, service: str) -> list[dict]:
        prefix = f"{SERVICES_PREFIX}{service}/"
        data = await self._post(
            "/v3/kv/range", {"key": _b64(prefix), "range_end": _b64(_prefix_end(prefix))}
        )
        return [json.loads(_unb64(kv["value"])) for kv in data.get("kvs", [])]

    async def get_config(self, service: str, key: str) -> str | None:
        data = await self._post(
            "/v3/kv/range", {"key": _b64(f"{CONFIG_PREFIX}{service}/{key}")}
        )
        kvs = data.get("kvs", [])
        return _unb64(kvs[0]["value"]) if kvs else None
