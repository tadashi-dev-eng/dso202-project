"""PostgreSQL access.

Each service owns its own database on the shared PostgreSQL StatefulSet
(database-per-service: no service reads another's tables). The pool is
created with a retry loop so a service started before the database is
ready waits instead of crash-looping, and the schema is applied
idempotently (``CREATE ... IF NOT EXISTS``) on startup.
"""

import asyncio
import logging

import asyncpg

from . import config

log = logging.getLogger(__name__)


def dsn() -> str:
    return (
        f"postgresql://{config.env('DB_USER')}:{config.env('DB_PASSWORD')}"
        f"@{config.env('DB_HOST', 'postgres')}:{config.env('DB_PORT', '5432')}"
        f"/{config.env('DB_NAME')}"
    )


async def connect(schema_sql: str | None = None, attempts: int = 60) -> asyncpg.Pool:
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            pool = await asyncpg.create_pool(
                dsn(),
                min_size=1,
                max_size=config.env_int("DB_POOL_SIZE", 5),
                command_timeout=10,
            )
            if schema_sql:
                async with pool.acquire() as conn:
                    # Advisory lock: two replicas starting together must not
                    # race each other through the same DDL.
                    await conn.execute("SELECT pg_advisory_lock(424242)")
                    try:
                        await conn.execute(schema_sql)
                    finally:
                        await conn.execute("SELECT pg_advisory_unlock(424242)")
            log.info("database connected", extra={"fields": {"attempt": attempt}})
            return pool
        except (OSError, asyncpg.PostgresError) as exc:
            log.warning(
                "database not ready, retrying",
                extra={"fields": {"attempt": attempt, "error": str(exc)}},
            )
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, 5.0)
    raise RuntimeError("database unreachable")


async def check(pool: asyncpg.Pool) -> None:
    """Readiness check: one round-trip."""
    async with pool.acquire() as conn:
        await conn.fetchval("SELECT 1")
