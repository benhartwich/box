"""Households created in the web UI (SPEC v0.6 §3.2)."""

from __future__ import annotations

import uuid

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import delete, select

from myboxi_server.domain import members
from myboxi_server.domain.errors import ConflictError, InvalidInputError
from myboxi_server.models import Membership
from myboxi_server.models.enums import Role

from .helpers import PASSWORD, csrf_from, fresh_window, login, make_tenant, sessionmaker_of


async def _user_id(app: FastAPI, email: str) -> uuid.UUID:
    async with sessionmaker_of(app)() as db:
        user = await members.user_by_email(db, email)
        assert user is not None
        return user.id


async def test_create_household_and_become_owner(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app, "Familie A")
    csrf = await login(client, t.owner_email)
    page = await client.get("/households")
    assert "Familie A" in page.text
    assert "Neuen Haushalt anlegen" in page.text
    r = await client.post("/households", data={"name": "  Oma und Opa ", "csrf_token": csrf})
    assert r.status_code == 303
    tid = uuid.UUID(r.headers["location"].split("/")[2])
    assert r.headers["location"] == f"/t/{tid}/setup"
    user_id = await _user_id(app, t.owner_email)
    async with sessionmaker_of(app)() as db:
        role = await db.scalar(
            select(Membership.role).where(
                Membership.tenant_id == tid, Membership.user_id == user_id
            )
        )
    assert role == Role.OWNER
    assert "Oma und Opa" in (await client.get("/households")).text


async def test_household_needs_login_csrf_and_a_name(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    r = await client.get("/households")
    assert r.status_code in (303, 401)
    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    r = await client.post("/households", data={"name": "Ohne Token"})
    assert r.status_code == 403
    r = await client.post("/households", data={"name": "   ", "csrf_token": csrf})
    assert r.status_code == 400
    assert "Namen" in r.text


async def test_household_rate_limit(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    await fresh_window(3600, 10)
    for i in range(5):
        r = await client.post("/households", data={"name": f"H{i}", "csrf_token": csrf})
        assert r.status_code == 303
    r = await client.post("/households", data={"name": "zu viel", "csrf_token": csrf})
    assert r.status_code == 429


async def test_at_most_ten_owned_households(app: FastAPI) -> None:
    t = await make_tenant(app)
    user_id = await _user_id(app, t.owner_email)
    async with sessionmaker_of(app)() as db:
        for i in range(members.MAX_OWNED_TENANTS - 1):
            await members.create_tenant(db, user_id, f"H{i}")
        with pytest.raises(ConflictError):
            await members.create_tenant(db, user_id, "elf")
        with pytest.raises(InvalidInputError):
            await members.create_tenant(db, user_id, "x" * 101)
        await db.rollback()


async def test_member_without_household_sees_the_form(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    t = await make_tenant(app)
    async with sessionmaker_of(app)() as db:
        await db.execute(delete(Membership).where(Membership.tenant_id == t.tenant_id))
        await db.commit()
    page = await client.get("/login")
    r = await client.post(
        "/login",
        data={"email": t.owner_email, "password": PASSWORD, "csrf_token": csrf_from(page.text)},
    )
    assert r.status_code == 303
    home = await client.get("/")
    assert "Leg einen an" in home.text
