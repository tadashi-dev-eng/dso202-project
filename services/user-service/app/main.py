"""user-service -- user profiles and preferences.

Profiles are created by auth-service at registration
(``POST /internal/profiles``). Other services read them through the
``/internal`` routes: list-service resolves an e-mail address when a list
is shared, notification-service looks up the recipient's name and
preferences.
"""

import json
import uuid

from fastapi import HTTPException
from pydantic import BaseModel, EmailStr, Field

from taskflow_common import create_service
from taskflow_common.auth import CurrentUser

SCHEMA = """
CREATE TABLE IF NOT EXISTS profiles (
    id           UUID PRIMARY KEY,
    email        TEXT NOT NULL UNIQUE,
    display_name TEXT NOT NULL,
    timezone     TEXT NOT NULL DEFAULT 'Asia/Thimphu',
    preferences  JSONB NOT NULL DEFAULT '{"email_notifications": true, "push_notifications": true}',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

app, ctx = create_service("user-service", schema=SCHEMA)


class NewProfile(BaseModel):
    id: uuid.UUID
    email: EmailStr
    display_name: str


class ProfileUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=80)
    timezone: str | None = Field(default=None, max_length=64)
    preferences: dict | None = None


def _out(row) -> dict:
    return {
        "id": str(row["id"]),
        "email": row["email"],
        "display_name": row["display_name"],
        "timezone": row["timezone"],
        "preferences": json.loads(row["preferences"]),
        "created_at": row["created_at"].isoformat(),
    }


@app.post("/internal/profiles", status_code=201)
async def create_profile(body: NewProfile):
    row = await ctx.db.fetchrow(
        "INSERT INTO profiles (id, email, display_name) VALUES ($1, $2, $3) "
        "ON CONFLICT (id) DO UPDATE SET email = EXCLUDED.email RETURNING *",
        body.id, body.email.lower(), body.display_name,
    )
    return _out(row)


@app.get("/internal/users/{user_id}")
async def get_internal(user_id: uuid.UUID):
    row = await ctx.db.fetchrow("SELECT * FROM profiles WHERE id = $1", user_id)
    if not row:
        raise HTTPException(404, "user not found")
    return _out(row)


@app.get("/users/me")
async def me(user: CurrentUser):
    return await get_internal(uuid.UUID(user))


@app.patch("/users/me")
async def update_me(body: ProfileUpdate, user: CurrentUser):
    current = await ctx.db.fetchrow("SELECT * FROM profiles WHERE id = $1", uuid.UUID(user))
    if not current:
        raise HTTPException(404, "user not found")
    prefs = json.loads(current["preferences"])
    if body.preferences:
        prefs.update(body.preferences)
    row = await ctx.db.fetchrow(
        "UPDATE profiles SET display_name = $2, timezone = $3, preferences = $4::jsonb, "
        "updated_at = now() WHERE id = $1 RETURNING *",
        uuid.UUID(user),
        body.display_name or current["display_name"],
        body.timezone or current["timezone"],
        json.dumps(prefs),
    )
    return _out(row)


@app.get("/users/lookup")
async def lookup(email: EmailStr, user: CurrentUser):
    """Find a user by e-mail (used when sharing a list). Returns the public
    part of the profile only."""
    row = await ctx.db.fetchrow("SELECT * FROM profiles WHERE email = $1", email.lower())
    if not row:
        raise HTTPException(404, "user not found")
    return {"id": str(row["id"]), "email": row["email"], "display_name": row["display_name"]}
