"""Test helpers: tenants, users, logged-in clients."""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

import httpx
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from box_server.auth.passwords import hash_secret
from box_server.domain.members import create_tenant_with_owner
from box_server.models import Membership, User
from box_server.models.enums import Role

PASSWORD = "correct horse battery"
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
# Hashing once keeps tests fast; argon2 is exercised by the login tests anyway.
_PASSWORD_HASH = hash_secret(PASSWORD)


def sessionmaker_of(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = app.state.sessionmaker
    return maker


@dataclass
class TenantFixture:
    tenant_id: uuid.UUID
    owner_email: str


async def make_tenant(app: FastAPI, name: str = "Familie Muster") -> TenantFixture:
    email = f"owner-{uuid.uuid4().hex[:8]}@example.org"
    async with sessionmaker_of(app)() as db:
        tenant, _ = await create_tenant_with_owner(
            db, tenant_name=name, email=email, display_name="Owner", password=PASSWORD
        )
        await db.commit()
        return TenantFixture(tenant.id, email)


async def add_member(app: FastAPI, tenant_id: uuid.UUID, role: Role) -> str:
    email = f"{role.value}-{uuid.uuid4().hex[:8]}@example.org"
    async with sessionmaker_of(app)() as db:
        user = User(email=email, display_name=role.value, password_hash=_PASSWORD_HASH)
        db.add(user)
        await db.flush()
        db.add(Membership(tenant_id=tenant_id, user_id=user.id, role=role))
        await db.commit()
    return email


def csrf_from(html: str) -> str:
    m = _CSRF_RE.search(html)
    assert m, "no csrf token in page"
    return m.group(1)


async def login(client: httpx.AsyncClient, email: str, password: str = PASSWORD) -> str:
    """Log in and return the session's CSRF token."""
    page = await client.get("/login")
    token = csrf_from(page.text)
    r = await client.post(
        "/login", data={"email": email, "password": password, "csrf_token": token}
    )
    assert r.status_code == 303, r.text
    home = await client.get("/", follow_redirects=True)
    return csrf_from(home.text)


def new_client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver")
