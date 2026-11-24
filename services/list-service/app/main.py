"""list-service -- todo lists and sharing.

list-service is the single authority on *who may touch which list*. A list
has members with a role (owner, editor, viewer). Other services never read
this table; they ask ``GET /internal/lists/{id}/access?user_id=...`` and get
the caller's role or 404.

Deleting a list publishes ``list.deleted``; task-service consumes it and
removes the list's tasks (eventual consistency instead of a cross-database
foreign key).
"""

import uuid
from typing import Literal

from fastapi import HTTPException
from pydantic import BaseModel, EmailStr, Field

from taskflow_common import create_service
from taskflow_common.auth import CurrentUser

SCHEMA = """
CREATE TABLE IF NOT EXISTS lists (
    id         UUID PRIMARY KEY,
    owner_id   UUID NOT NULL,
    name       TEXT NOT NULL,
    color      TEXT NOT NULL DEFAULT '#4f46e5',
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS list_members (
    list_id  UUID NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    user_id  UUID NOT NULL,
    role     TEXT NOT NULL CHECK (role IN ('owner', 'editor', 'viewer')),
    added_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (list_id, user_id)
);
CREATE INDEX IF NOT EXISTS list_members_user ON list_members(user_id);
"""

app, ctx = create_service("list-service", schema=SCHEMA, use_nats=True)

Role = Literal["owner", "editor", "viewer"]
WRITE_ROLES = {"owner", "editor"}


class NewList(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    color: str = Field(default="#4f46e5", pattern=r"^#[0-9a-fA-F]{6}$")


class ListUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    color: str | None = Field(default=None, pattern=r"^#[0-9a-fA-F]{6}$")


class NewMember(BaseModel):
    email: EmailStr
    role: Literal["editor", "viewer"] = "editor"


async def _role(list_id: uuid.UUID, user_id: str) -> str | None:
    return await ctx.db.fetchval(
        "SELECT role FROM list_members WHERE list_id = $1 AND user_id = $2",
        list_id, uuid.UUID(user_id),
    )


async def _require(list_id: uuid.UUID, user_id: str, roles: set[str] | None = None) -> str:
    role = await _role(list_id, user_id)
    if role is None:
        raise HTTPException(404, "list not found")
    if roles and role not in roles:
        raise HTTPException(403, f"requires role {sorted(roles)}")
    return role


def _out(row, role: str | None = None) -> dict:
    doc = {
        "id": str(row["id"]),
        "owner_id": str(row["owner_id"]),
        "name": row["name"],
        "color": row["color"],
        "created_at": row["created_at"].isoformat(),
    }
    if role:
        doc["role"] = role
    return doc


@app.get("/lists")
async def my_lists(user: CurrentUser):
    rows = await ctx.db.fetch(
        "SELECT l.*, m.role FROM lists l JOIN list_members m ON m.list_id = l.id "
        "WHERE m.user_id = $1 ORDER BY l.created_at",
        uuid.UUID(user),
    )
    return [_out(r, r["role"]) for r in rows]


@app.post("/lists", status_code=201)
async def create_list(body: NewList, user: CurrentUser):
    list_id = uuid.uuid4()
    async with ctx.db.acquire() as conn, conn.transaction():
        row = await conn.fetchrow(
            "INSERT INTO lists (id, owner_id, name, color) VALUES ($1, $2, $3, $4) RETURNING *",
            list_id, uuid.UUID(user), body.name, body.color,
        )
        await conn.execute(
            "INSERT INTO list_members (list_id, user_id, role) VALUES ($1, $2, 'owner')",
            list_id, uuid.UUID(user),
        )
    out = _out(row, "owner")
    await ctx.bus.publish("list.created", {"list": out, "actor_id": user})
    return out


@app.get("/lists/{list_id}")
async def get_list(list_id: uuid.UUID, user: CurrentUser):
    role = await _require(list_id, user)
    row = await ctx.db.fetchrow("SELECT * FROM lists WHERE id = $1", list_id)
    members = await ctx.db.fetch(
        "SELECT user_id, role FROM list_members WHERE list_id = $1 ORDER BY added_at", list_id
    )
    out = _out(row, role)
    out["members"] = [{"user_id": str(m["user_id"]), "role": m["role"]} for m in members]
    return out


@app.patch("/lists/{list_id}")
async def update_list(list_id: uuid.UUID, body: ListUpdate, user: CurrentUser):
    role = await _require(list_id, user, WRITE_ROLES)
    row = await ctx.db.fetchrow(
        "UPDATE lists SET name = COALESCE($2, name), color = COALESCE($3, color) "
        "WHERE id = $1 RETURNING *",
        list_id, body.name, body.color,
    )
    out = _out(row, role)
    await ctx.bus.publish("list.updated", {"list": out, "actor_id": user})
    return out


@app.delete("/lists/{list_id}", status_code=204)
async def delete_list(list_id: uuid.UUID, user: CurrentUser):
    await _require(list_id, user, {"owner"})
    await ctx.db.execute("DELETE FROM lists WHERE id = $1", list_id)
    await ctx.bus.publish("list.deleted", {"list_id": str(list_id), "actor_id": user})


@app.post("/lists/{list_id}/members", status_code=201)
async def add_member(list_id: uuid.UUID, body: NewMember, user: CurrentUser):
    await _require(list_id, user, {"owner"})
    resp = await ctx.call("user-service", "GET", "/users/lookup",
                          user_id=user, params={"email": body.email})
    if resp.status_code == 404:
        raise HTTPException(404, "no user with that e-mail")
    resp.raise_for_status()
    member = resp.json()
    await ctx.db.execute(
        "INSERT INTO list_members (list_id, user_id, role) VALUES ($1, $2, $3) "
        "ON CONFLICT (list_id, user_id) DO UPDATE SET role = EXCLUDED.role "
        "WHERE list_members.role <> 'owner'",
        list_id, uuid.UUID(member["id"]), body.role,
    )
    await ctx.bus.publish("list.shared", {
        "list_id": str(list_id), "user_id": member["id"], "role": body.role, "actor_id": user,
    })
    return {"user_id": member["id"], "email": member["email"], "role": body.role}


@app.delete("/lists/{list_id}/members/{member_id}", status_code=204)
async def remove_member(list_id: uuid.UUID, member_id: uuid.UUID, user: CurrentUser):
    await _require(list_id, user, {"owner"})
    await ctx.db.execute(
        "DELETE FROM list_members WHERE list_id = $1 AND user_id = $2 AND role <> 'owner'",
        list_id, member_id,
    )


# --- internal API for other services --------------------------------------

@app.get("/internal/lists/{list_id}/access")
async def access(list_id: uuid.UUID, user_id: uuid.UUID):
    role = await _role(list_id, str(user_id))
    if role is None:
        raise HTTPException(404, "no access")
    return {"list_id": str(list_id), "user_id": str(user_id), "role": role,
            "can_write": role in WRITE_ROLES}


@app.get("/internal/lists/{list_id}/members")
async def members(list_id: uuid.UUID):
    rows = await ctx.db.fetch("SELECT user_id, role FROM list_members WHERE list_id = $1", list_id)
    return [{"user_id": str(r["user_id"]), "role": r["role"]} for r in rows]
