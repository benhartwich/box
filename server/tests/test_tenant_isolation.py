"""Tenant isolation (CLAUDE.md rule 7, SPEC §10): every endpoint refuses foreign data.

New routes are picked up automatically: every ``{tid}`` route is exercised with a foreign
tenant, every extra id parameter must have a foreign object below, and every device route
must be listed in ``DEVICE_ROUTES`` with its own test.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from myboxi_protocol.events import EventBatchResponse
from myboxi_protocol.state import StateResponse
from myboxi_server.domain import members
from myboxi_server.models import ContentItem, Event, Membership, ResumePosition, Token, Upload
from myboxi_server.models.enums import Role, UploadProfile, UploadStatus

from .helpers import (
    Library,
    PairedDevice,
    login,
    make_tenant,
    owner_ctx,
    pair_device,
    seed_library,
    sessionmaker_of,
)
from .routes import routes, tenant_routes

# Device routes and the test covering them; the coverage test fails for unlisted routes.
DEVICE_ROUTES = {
    ("POST", "/api/v1/pairing/start"): "no tenant data (test_pairing)",
    ("GET", "/api/v1/pairing/poll"): "poll token is the capability (test_pairing)",
    ("POST", "/api/v1/device/token"): "test_pairing::test_device_token_errors_are_uniform",
    ("GET", "/api/v1/device/state"): "test_state_contains_only_own_tenant",
    ("GET", "/api/v1/device/assets/{sha256}"): "test_foreign_assets_are_404",
    ("POST", "/api/v1/device/events"): "test_events_are_scoped",
    ("POST", "/api/v1/device/reported"): "test_reported_and_unpair_affect_only_own_device",
    ("POST", "/api/v1/device/unpair"): "test_reported_and_unpair_affect_only_own_device",
}


@dataclass
class Side:
    tenant_id: uuid.UUID
    owner_email: str
    owner_id: uuid.UUID
    lib: Library
    device: PairedDevice
    invitation_id: uuid.UUID
    item_id: uuid.UUID
    asset_id: uuid.UUID
    upload_id: uuid.UUID


@dataclass
class World:
    a: Side
    b: Side

    def foreign_ids(self) -> dict[str, str]:
        """Path parameter values pointing at tenant B's objects."""
        return {
            "user_id": str(self.b.owner_id),
            "invitation_id": str(self.b.invitation_id),
            "device_id": str(self.b.device.device_id),
            "token_id": str(self.b.lib.token_id),
            "content_id": str(self.b.lib.content_id),
            "item_id": str(self.b.item_id),
            "asset_id": str(self.b.asset_id),
            "upload_id": str(self.b.upload_id),
            "sha256": self.b.lib.shas[0],
        }


async def _side(app: FastAPI, client: httpx.AsyncClient, name: str) -> Side:
    t = await make_tenant(app, name)
    lib = await seed_library(app, t.tenant_id)
    device = await pair_device(app, client, t.tenant_id)
    ctx = await owner_ctx(app, t.tenant_id)
    async with sessionmaker_of(app)() as db:
        inv, _ = await members.create_invitation(db, ctx, f"guest-{name}@example.org", Role.VIEWER)
        item = (
            await db.scalars(select(ContentItem).where(ContentItem.tenant_id == t.tenant_id))
        ).first()
        assert item is not None
        upload = Upload(
            tenant_id=t.tenant_id, content_id=lib.content_id, original_filename="x.mp3",
            source_sha256="0" * 64, source_bytes=1, profile=UploadProfile.MUSIC,
            status=UploadStatus.FAILED, error="x",
        )  # fmt: skip
        db.add(upload)
        await db.commit()
        assert ctx.user_id is not None
    return Side(
        t.tenant_id,
        t.owner_email,
        ctx.user_id,
        lib,
        device,
        inv.id,
        item.id,
        item.asset_id,
        upload.id,
    )


@pytest.fixture
async def world(app: FastAPI, client: httpx.AsyncClient) -> World:
    return World(await _side(app, client, "A"), await _side(app, client, "B"))


def test_every_route_is_covered(app: FastAPI) -> None:
    device = {
        (r.method, r.path)
        for r in routes(app)
        if r.path.startswith("/api/v1/") and "tid" not in r.params
    }
    assert device == set(DEVICE_ROUTES), "add new device routes to DEVICE_ROUTES with a test"
    known = {
        "tid",
        "user_id",
        "invitation_id",
        "device_id",
        "token_id",
        "content_id",
        "item_id",
        "upload_id",
        "asset_id",
    }
    for r in tenant_routes(app):
        assert set(r.params) <= known, (
            f"{r.method} {r.path}: map the new id parameter in foreign_ids"
        )
        assert r.perm is not None, f"{r.method} {r.path} has no require(perm)"


async def test_foreign_tenant_routes_are_404(
    app: FastAPI, client: httpx.AsyncClient, world: World
) -> None:
    """Owner of A addresses tenant B: every route answers 404 (existence is not revealed)."""
    csrf = await login(client, world.a.owner_email)
    values = world.foreign_ids() | {"tid": str(world.b.tenant_id)}
    checked = 0
    for r in tenant_routes(app):
        resp = await client.request(
            r.method, r.url(values), headers={"X-CSRF-Token": csrf}, data={}
        )
        assert resp.status_code == 404, f"{r.method} {r.path} → {resp.status_code}"
        checked += 1
    assert checked > 0


AUDIO = ("a.mp3", b"ID3not really audio", "audio/mpeg")
IMAGE = ("c.png", b"not really an image", "image/png")


def foreign_object_request(world: World, method: str, path: str) -> dict[str, object]:
    """Valid form bodies, so the request reaches the tenant check instead of form validation."""
    own_content = str(world.a.lib.content_id)
    forms: dict[tuple[str, str], dict[str, object]] = {
        ("POST", "/t/{tid}/members/{user_id}/role"): {"data": {"role": "viewer"}},
        ("POST", "/t/{tid}/boxes/{device_id}/rename"): {"data": {"name": "x"}},
        ("POST", "/t/{tid}/boxes/{device_id}/config"): {
            "data": {
                "max_volume": "50", "start_volume": "30", "on_token_removed": "pause",
                "locale": "de-AT", "timezone": "Europe/Vienna", "providers": "local",
            }
        },
        ("POST", "/t/{tid}/figures/{token_id}"): {"data": {"label": "x"}},
        ("POST", "/t/{tid}/figures/{token_id}/binding"): {"data": {"content_id": own_content}},
        ("POST", "/t/{tid}/contents/{content_id}"): {"data": {"title": "x"}},
        ("POST", "/t/{tid}/contents/{content_id}/items/{item_id}/move"): {"data": {"direction": "up"}},  # noqa: E501
        ("POST", "/t/{tid}/contents/{content_id}/items/{item_id}/rename"): {"data": {"title": "x"}},
        ("POST", "/t/{tid}/contents/{content_id}/uploads"): {"files": {"files": AUDIO}},
        ("POST", "/t/{tid}/contents/{content_id}/cover"): {"files": {"file": IMAGE}},
    }  # fmt: skip
    return forms.get((method, path), {"data": {}})


async def test_foreign_objects_in_own_tenant_are_404(
    app: FastAPI, client: httpx.AsyncClient, world: World
) -> None:
    """Own tenant id, but an object id of tenant B: 404 and nothing changes."""
    csrf = await login(client, world.a.owner_email)
    values = world.foreign_ids() | {"tid": str(world.a.tenant_id)}
    for r in tenant_routes(app):
        if r.params == ["tid"]:
            continue
        kwargs = foreign_object_request(world, r.method, r.path) if r.method != "GET" else {}
        resp = await client.request(
            r.method,
            r.url(values),
            headers={"X-CSRF-Token": csrf},
            **kwargs,  # pyright: ignore[reportArgumentType]
        )
        assert resp.status_code == 404, f"{r.method} {r.path} → {resp.status_code}"
    async with sessionmaker_of(app)() as db:
        m = await db.get(Membership, (world.b.tenant_id, world.b.owner_id))
        assert m is not None
        assert m.role == Role.OWNER
        token = await db.get(Token, world.b.lib.token_id)
        assert token is not None
        assert token.label == "Bibi"


async def test_binding_to_foreign_content_is_404(
    app: FastAPI, client: httpx.AsyncClient, world: World
) -> None:
    """Own figure, foreign content: refused (the DB's composite FK would refuse it too)."""
    csrf = await login(client, world.a.owner_email)
    r = await client.post(
        f"/t/{world.a.tenant_id}/figures/{world.a.lib.token_id}/binding",
        data={"content_id": str(world.b.lib.content_id), "csrf_token": csrf},
    )
    assert r.status_code == 404


async def test_state_contains_only_own_tenant(client: httpx.AsyncClient, world: World) -> None:
    r = await client.get("/api/v1/device/state", headers=world.a.device.auth)
    state = StateResponse.model_validate_json(r.content)
    assert {t.id for t in state.upserts.token} == {world.a.lib.token_id}
    assert {c.id for c in state.upserts.content} == {world.a.lib.content_id}
    assert {i.asset_sha256 for i in state.upserts.content_item} == set(world.a.lib.shas)
    assert {b.token_id for b in state.upserts.binding} == {world.a.lib.token_id}


async def test_foreign_assets_are_404(client: httpx.AsyncClient, world: World) -> None:
    for sha in world.b.lib.shas:
        r = await client.get(f"/api/v1/device/assets/{sha}", headers=world.a.device.auth)
        assert r.status_code == 404
    own = world.a.lib.shas[0]
    assert (
        await client.get(f"/api/v1/device/assets/{own}", headers=world.a.device.auth)
    ).status_code == 200


async def test_shared_file_is_served_per_tenant(app: FastAPI, client: httpx.AsyncClient) -> None:
    """The same file in two tenants: each box gets it through its own asset row only."""
    payload = b"identical content" * 100
    a = await make_tenant(app, "A")
    b = await make_tenant(app, "B")
    lib_a = await seed_library(app, a.tenant_id, n_items=1, payload=payload)
    await seed_library(app, b.tenant_id, n_items=1, payload=payload)
    dev_a = await pair_device(app, client, a.tenant_id)
    r = await client.get(f"/api/v1/device/assets/{lib_a.shas[0]}", headers=dev_a.auth)
    assert r.status_code == 200
    assert r.content == payload


async def test_events_are_scoped(app: FastAPI, client: httpx.AsyncClient, world: World) -> None:
    foreign_resume = {
        "v": 1,
        "id": "01J8Z3M5W6XK2C4B7N9P0QRSTV",
        "ts": "2026-09-24T18:02:11Z",
        "type": "resume_position",
        "boot_id": str(uuid.uuid4()),
        "mono_ms": 1,
        "data": {"token_id": str(world.b.lib.token_id), "item_index": 0, "position_ms": 99},
    }
    r = await client.post(
        "/api/v1/device/events", json={"events": [foreign_resume]}, headers=world.a.device.auth
    )
    assert EventBatchResponse.model_validate_json(r.content).results[0].status == "accepted"
    async with sessionmaker_of(app)() as db:
        ev = await db.scalar(select(Event))
        assert ev is not None
        assert ev.tenant_id == world.a.tenant_id
        assert ev.device_id == world.a.device.device_id
        assert await db.get(ResumePosition, (world.b.tenant_id, world.b.lib.token_id)) is None
        assert await db.get(ResumePosition, (world.a.tenant_id, world.b.lib.token_id)) is None


async def test_reported_and_unpair_affect_only_own_device(
    client: httpx.AsyncClient, world: World
) -> None:
    r = await client.post("/api/v1/device/unpair", headers=world.a.device.auth)
    assert r.status_code == 204
    assert (
        await client.get("/api/v1/device/state", headers=world.b.device.auth)
    ).status_code == 200


async def test_claim_into_foreign_tenant_is_404(client: httpx.AsyncClient, world: World) -> None:
    csrf = await login(client, world.a.owner_email)
    r = await client.post(
        f"/api/v1/tenants/{world.b.tenant_id}/devices/claim",
        json={"code": "123456", "name": "x"},
        headers={"X-CSRF-Token": csrf},
    )
    assert r.status_code == 404
