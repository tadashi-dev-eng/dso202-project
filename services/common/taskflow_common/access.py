"""Authorisation helpers shared by the services that hang data off a task
or a list (tasks, tags, comments, attachments, reminders, recurrences).

The rule lives in one place: list-service decides list roles, task-service
maps a task to its list. These helpers ask them and cache the answer for a
few seconds, which keeps a busy page from turning every request into two
extra network calls while still noticing a revoked share quickly.
"""

import time
import uuid

from fastapi import HTTPException

from .config import env_float

_TTL = env_float("ACCESS_CACHE_TTL_SECONDS", 5.0)
_cache: dict[tuple, tuple[float, dict | None]] = {}


def _cached(key: tuple):
    hit = _cache.get(key)
    if hit and hit[0] > time.monotonic():
        return True, hit[1]
    return False, None


def _store(key: tuple, value: dict | None) -> None:
    if len(_cache) > 10_000:  # bound memory under a load test
        _cache.clear()
    _cache[key] = (time.monotonic() + _TTL, value)


async def list_access(ctx, list_id, user_id: str) -> dict | None:
    key = ("list", str(list_id), user_id)
    found, value = _cached(key)
    if found:
        return value
    resp = await ctx.call("list-service", "GET", f"/internal/lists/{list_id}/access",
                          params={"user_id": user_id})
    if resp.status_code == 404:
        value = None
    else:
        resp.raise_for_status()
        value = resp.json()
    _store(key, value)
    return value


async def task_access(ctx, task_id, user_id: str) -> dict | None:
    key = ("task", str(task_id), user_id)
    found, value = _cached(key)
    if found:
        return value
    resp = await ctx.call("task-service", "GET", f"/internal/tasks/{task_id}/access",
                          params={"user_id": user_id})
    if resp.status_code == 404:
        value = None
    else:
        resp.raise_for_status()
        value = resp.json()
    _store(key, value)
    return value


async def require_list(ctx, list_id, user_id: str, write: bool = False) -> dict:
    acc = await list_access(ctx, list_id, user_id)
    if acc is None:
        raise HTTPException(404, "list not found")
    if write and not acc["can_write"]:
        raise HTTPException(403, "read-only access to this list")
    return acc


async def require_task(ctx, task_id: uuid.UUID, user_id: str, write: bool = False) -> dict:
    acc = await task_access(ctx, task_id, user_id)
    if acc is None:
        raise HTTPException(404, "task not found")
    if write and not acc["can_write"]:
        raise HTTPException(403, "read-only access to this task")
    return acc
