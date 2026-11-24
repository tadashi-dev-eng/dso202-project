"""Cached profile lookups (display names, notification preferences).

Comments, notifications and the audit log show people by name. The name
belongs to user-service, so it is fetched through its internal API and
cached for a minute rather than copied into every service's database.
"""

import time

_TTL = 60.0
_cache: dict[str, tuple[float, dict | None]] = {}


async def profile(ctx, user_id: str | None) -> dict | None:
    if not user_id:
        return None
    hit = _cache.get(user_id)
    if hit and hit[0] > time.monotonic():
        return hit[1]
    try:
        resp = await ctx.call("user-service", "GET", f"/internal/users/{user_id}")
        value = resp.json() if resp.status_code == 200 else None
    except Exception:  # noqa: BLE001 -- a name is cosmetic; never fail the caller
        value = None
    if len(_cache) > 10_000:
        _cache.clear()
    _cache[user_id] = (time.monotonic() + _TTL, value)
    return value


async def display_name(ctx, user_id: str | None) -> str | None:
    p = await profile(ctx, user_id)
    return p["display_name"] if p else None
