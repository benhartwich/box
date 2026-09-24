"""Test helpers: tenants, users, logged-in clients, paired devices, seeded libraries."""

from __future__ import annotations

import hashlib
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

import httpx
import sqlalchemy as sa
from fastapi import FastAPI
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from box_protocol.pairing import PairingClaimed, PairingStartResponse
from box_server.auth.passwords import hash_secret
from box_server.domain import pairing as pairing_domain
from box_server.domain.authz import TenantContext
from box_server.domain.members import create_tenant_with_owner
from box_server.models import (
    Asset,
    Binding,
    Content,
    ContentItem,
    Membership,
    Pairing,
    Token,
    User,
)
from box_server.models.enums import ContentKind, Role
from box_server.storage.base import relpath_for
from box_server.storage.filesystem import FilesystemAssetStore

PASSWORD = "correct horse battery"
OPUS_MIME = "audio/ogg; codecs=opus"
_CSRF_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
# Hashing once keeps tests fast; argon2 is exercised by the login tests anyway.
_PASSWORD_HASH = hash_secret(PASSWORD)


def sessionmaker_of(app: FastAPI) -> async_sessionmaker[AsyncSession]:
    maker: async_sessionmaker[AsyncSession] = app.state.sessionmaker
    return maker


# --- tenants and users ---------------------------------------------------------------------


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


async def owner_ctx(app: FastAPI, tenant_id: uuid.UUID) -> TenantContext:
    async with sessionmaker_of(app)() as db:
        user_id = await db.scalar(
            sa.select(Membership.user_id).where(
                Membership.tenant_id == tenant_id, Membership.role == Role.OWNER
            )
        )
    return TenantContext(tenant_id, user_id, Role.OWNER)


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


# --- devices -------------------------------------------------------------------------------


@dataclass
class PairedDevice:
    device_id: uuid.UUID
    tenant_id: uuid.UUID
    secret: str
    access_token: str

    @property
    def auth(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}


async def start_pairing(
    client: httpx.AsyncClient, device_id: uuid.UUID | None = None
) -> PairingStartResponse:
    body = {"device_id": str(device_id or uuid.uuid4()), "hw_model": "rpi4", "agent_version": "0.1"}
    r = await client.post("/api/v1/pairing/start", json=body)
    assert r.status_code == 200, r.text
    return PairingStartResponse.model_validate_json(r.content)


async def claim_code(
    app: FastAPI, tenant_id: uuid.UUID, code: str, name: str = "Kinderzimmer"
) -> None:
    ctx = await owner_ctx(app, tenant_id)
    async with sessionmaker_of(app)() as db:
        await pairing_domain.claim(db, ctx, code=code, name=name)
        await db.commit()


async def device_of_code(app: FastAPI, code: str) -> uuid.UUID:
    async with sessionmaker_of(app)() as db:
        device_id = await db.scalar(
            sa.select(Pairing.device_id)
            .where(Pairing.code == code)
            .order_by(Pairing.created_at.desc())
        )
    assert device_id is not None
    return device_id


async def pair_device(
    app: FastAPI,
    client: httpx.AsyncClient,
    tenant_id: uuid.UUID,
    device_id: uuid.UUID | None = None,
) -> PairedDevice:
    started = await start_pairing(client, device_id)
    await claim_code(app, tenant_id, started.code)
    r = await client.get("/api/v1/pairing/poll", params={"poll_token": started.poll_token})
    assert r.status_code == 200, r.text
    claimed = PairingClaimed.model_validate_json(r.content)
    dev = await device_of_code(app, started.code)
    body = {"device_id": str(dev), "device_secret": claimed.device_secret}
    r = await client.post("/api/v1/device/token", json=body)
    assert r.status_code == 200, r.text
    return PairedDevice(dev, tenant_id, claimed.device_secret, r.json()["access_token"])


# --- library -------------------------------------------------------------------------------


@dataclass
class Library:
    token_id: uuid.UUID
    content_id: uuid.UUID
    shas: list[str]
    token_uid: str


async def seed_library(
    app: FastAPI, tenant_id: uuid.UUID, n_items: int = 2, payload: bytes | None = None
) -> Library:
    """A figure bound to a collection with ``n_items`` stored assets."""
    store: FilesystemAssetStore = app.state.asset_store
    uid = os.urandom(7).hex().upper()
    async with sessionmaker_of(app)() as db:
        token = Token(tenant_id=tenant_id, uid=uid, label="Bibi")
        content = Content(
            tenant_id=tenant_id, kind=ContentKind.COLLECTION, title="Folge 1", source={}
        )
        db.add_all([token, content])
        await db.flush()
        shas: list[str] = []
        for i in range(n_items):
            data = payload if payload is not None else os.urandom(4096 + i)
            sha = hashlib.sha256(data).hexdigest()
            rel = relpath_for(sha, "opus")
            path = store.path(rel)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            asset = Asset(
                tenant_id=tenant_id,
                sha256=sha,
                mime=OPUS_MIME,
                bytes=len(data),
                storage_path=rel,
                duration_ms=1000,
            )
            db.add(asset)
            await db.flush()
            db.add(
                ContentItem(
                    tenant_id=tenant_id,
                    content_id=content.id,
                    position=i,
                    asset_id=asset.id,
                    title=f"Teil {i + 1}",
                    duration_ms=1000,
                )
            )
            shas.append(sha)
        db.add(Binding(tenant_id=tenant_id, token_id=token.id, content_id=content.id))
        await db.commit()
        return Library(token.id, content.id, shas, uid)


def asset_file(app: FastAPI, sha: str) -> Path:
    store: FilesystemAssetStore = app.state.asset_store
    return store.path(relpath_for(sha, "opus"))
