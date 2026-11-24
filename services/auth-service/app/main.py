"""auth-service -- registration, login, token issuing and refresh.

Tokens:
* access token: JWT (HS256), 15 minutes, ``sub`` = user id. api-gateway
  verifies it with the same ``JWT_SECRET`` (a Kubernetes Secret shared by
  exactly these two Deployments).
* refresh token: random 256-bit string, stored only as a SHA-256 hash,
  rotated on every use (an old refresh token cannot be replayed).

On registration the profile is created in user-service with a synchronous
call, so the user exists everywhere before the first token is returned.
"""

import asyncio
import hashlib
import secrets
import time
import uuid

import bcrypt
import jwt
from fastapi import HTTPException
from prometheus_client import Counter
from pydantic import BaseModel, EmailStr, Field

from taskflow_common import create_service
from taskflow_common.config import env, env_int

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            UUID PRIMARY KEY,
    email         TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS refresh_tokens (
    token_hash TEXT PRIMARY KEY,
    user_id    UUID NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    expires_at TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS refresh_tokens_user ON refresh_tokens(user_id);
"""

app, ctx = create_service("auth-service", schema=SCHEMA)

JWT_SECRET = env("JWT_SECRET", "dev-only-secret")
ACCESS_TTL = env_int("ACCESS_TOKEN_TTL_SECONDS", 900)
REFRESH_TTL = env_int("REFRESH_TOKEN_TTL_SECONDS", 7 * 24 * 3600)
# Cost 10 (~60 ms) keeps login usable under the load test on a laptop;
# production would use 12 or more.
BCRYPT_ROUNDS = env_int("BCRYPT_ROUNDS", 10)

AUTH_EVENTS = Counter("taskflow_auth_events_total", "Auth outcomes", ["event", "result"])


class Register(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128)
    display_name: str = Field(min_length=1, max_length=80)


class Login(BaseModel):
    email: EmailStr
    password: str


class Refresh(BaseModel):
    refresh_token: str


def _hash_refresh(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def _issue(user_id: str, email: str) -> dict:
    now = int(time.time())
    access = jwt.encode(
        {"sub": user_id, "email": email, "iat": now, "exp": now + ACCESS_TTL, "iss": "taskflow"},
        JWT_SECRET,
        algorithm="HS256",
    )
    refresh = secrets.token_urlsafe(32)
    await ctx.db.execute(
        "INSERT INTO refresh_tokens (token_hash, user_id, expires_at) "
        "VALUES ($1, $2, now() + make_interval(secs => $3))",
        _hash_refresh(refresh), uuid.UUID(user_id), REFRESH_TTL,
    )
    return {
        "access_token": access,
        "refresh_token": refresh,
        "token_type": "bearer",
        "expires_in": ACCESS_TTL,
        "user": {"id": user_id, "email": email},
    }


@app.post("/auth/register", status_code=201)
async def register(body: Register):
    email = body.email.lower()
    if await ctx.db.fetchval("SELECT 1 FROM users WHERE email = $1", email):
        AUTH_EVENTS.labels("register", "conflict").inc()
        raise HTTPException(409, "email already registered")
    # bcrypt is CPU-bound: run it off the event loop.
    pw_hash = await asyncio.to_thread(
        lambda: bcrypt.hashpw(body.password.encode(), bcrypt.gensalt(BCRYPT_ROUNDS)).decode()
    )
    user_id = str(uuid.uuid4())
    await ctx.db.execute(
        "INSERT INTO users (id, email, password_hash) VALUES ($1, $2, $3)",
        uuid.UUID(user_id), email, pw_hash,
    )
    resp = await ctx.call(
        "user-service", "POST", "/internal/profiles",
        json={"id": user_id, "email": email, "display_name": body.display_name},
    )
    if resp.status_code >= 300:
        # Keep the two services consistent: no login without a profile.
        await ctx.db.execute("DELETE FROM users WHERE id = $1", uuid.UUID(user_id))
        AUTH_EVENTS.labels("register", "error").inc()
        raise HTTPException(502, "could not create profile")
    AUTH_EVENTS.labels("register", "ok").inc()
    ctx.log.info("user registered", extra={"fields": {"user_id": user_id}})
    return await _issue(user_id, email)


@app.post("/auth/login")
async def login(body: Login):
    row = await ctx.db.fetchrow(
        "SELECT id, email, password_hash FROM users WHERE email = $1", body.email.lower()
    )
    ok = row is not None and await asyncio.to_thread(
        bcrypt.checkpw, body.password.encode(), row["password_hash"].encode()
    )
    if not ok:
        AUTH_EVENTS.labels("login", "denied").inc()
        raise HTTPException(401, "invalid email or password")
    AUTH_EVENTS.labels("login", "ok").inc()
    return await _issue(str(row["id"]), row["email"])


@app.post("/auth/refresh")
async def refresh(body: Refresh):
    row = await ctx.db.fetchrow(
        "DELETE FROM refresh_tokens WHERE token_hash = $1 AND expires_at > now() "
        "RETURNING user_id",
        _hash_refresh(body.refresh_token),
    )
    if row is None:
        AUTH_EVENTS.labels("refresh", "denied").inc()
        raise HTTPException(401, "invalid refresh token")
    email = await ctx.db.fetchval("SELECT email FROM users WHERE id = $1", row["user_id"])
    AUTH_EVENTS.labels("refresh", "ok").inc()
    return await _issue(str(row["user_id"]), email)


@app.post("/auth/logout", status_code=204)
async def logout(body: Refresh):
    await ctx.db.execute(
        "DELETE FROM refresh_tokens WHERE token_hash = $1", _hash_refresh(body.refresh_token)
    )
