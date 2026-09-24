"""Device endpoints after pairing (SPEC §5.4, §7.3)."""

from __future__ import annotations

import datetime as dt
import hashlib
import uuid
from typing import Any

import httpx
from fastapi import FastAPI
from sqlalchemy import select, update

from box_protocol.errors import ErrorCode, ErrorResponse
from box_protocol.events import EventBatchResponse
from box_protocol.state import StateResponse
from box_server.models import Device, DeviceConfig, Event, ResumePosition, Tenant
from box_server.storage.filesystem import FilesystemAssetStore

from .helpers import asset_file, make_tenant, pair_device, seed_library, sessionmaker_of

BOOT = str(uuid.uuid4())
_n = 0


def ulid() -> str:
    global _n
    _n += 1
    return f"01J8Z3M5W6XK2C4B7N9P{_n:06d}"


def event(
    type_: str, data: dict[str, Any], *, id_: str | None = None, ts: str | None = None
) -> dict[str, Any]:
    return {
        "v": 1,
        "id": id_ or ulid(),
        "ts": ts or "2026-09-24T18:02:11Z",
        "type": type_,
        "boot_id": BOOT,
        "mono_ms": 1000,
        "data": data,
    }


async def test_state_is_full_snapshot(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    lib = await seed_library(app, t.tenant_id, n_items=3)
    dev = await pair_device(app, client, t.tenant_id)
    r = await client.get(
        "/api/v1/device/state", params={"config_rev": 5, "device_rev": 1}, headers=dev.auth
    )
    assert r.status_code == 200
    state = StateResponse.model_validate_json(r.content)
    assert state.full is True
    assert "deletes" not in r.json()
    async with sessionmaker_of(app)() as db:
        tenant = await db.get(Tenant, t.tenant_id)
        device = await db.get(Device, dev.device_id)
        assert tenant is not None
        assert device is not None
        assert state.config_rev == tenant.config_rev
        assert state.device_rev == device.device_rev == 1
    assert [tok.uid for tok in state.upserts.token] == [lib.token_uid]
    assert [c.id for c in state.upserts.content] == [lib.content_id]
    assert state.upserts.content[0].source.model_dump() == {}
    assert [i.asset_sha256 for i in state.upserts.content_item] == lib.shas
    assert [i.position for i in state.upserts.content_item] == [0, 1, 2]
    assert all(i.bytes > 0 for i in state.upserts.content_item)
    b = state.upserts.binding[0]
    assert (b.token_id, b.content_id, b.resume, b.shuffle, b.repeat) == (
        lib.token_id,
        lib.content_id,
        True,
        False,
        "off",
    )
    assert state.device_config.max_volume == 55
    assert state.device_config.timezone == "Europe/Vienna"


async def test_state_reflects_device_config(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    async with sessionmaker_of(app)() as db:
        await db.execute(
            update(DeviceConfig)
            .where(DeviceConfig.device_id == dev.device_id)
            .values(
                max_volume=40,
                quiet_hours={"start": "19:30", "end": "06:30", "max_volume": 20},
                providers_enabled=["local"],
            )
        )
        await db.commit()
    state = StateResponse.model_validate_json(
        (await client.get("/api/v1/device/state", headers=dev.auth)).content
    )
    assert state.device_rev == 2
    assert state.device_config.max_volume == 40
    assert state.device_config.quiet_hours is not None
    assert state.device_config.quiet_hours.max_volume == 20
    assert state.device_config.providers_enabled == ["local"]


async def test_asset_download_etag_and_range(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    lib = await seed_library(app, t.tenant_id, n_items=1)
    dev = await pair_device(app, client, t.tenant_id)
    sha = lib.shas[0]
    url = f"/api/v1/device/assets/{sha}"
    r = await client.get(url, headers=dev.auth)
    assert r.status_code == 200
    assert hashlib.sha256(r.content).hexdigest() == sha
    assert r.headers["etag"] == f'"{sha}"'
    assert r.headers["content-type"].startswith("audio/ogg")
    assert "immutable" in r.headers["cache-control"]
    r = await client.get(url, headers=dev.auth | {"Range": "bytes=10-19"})
    assert r.status_code == 206
    assert r.content == asset_file(app, sha).read_bytes()[10:20]
    r = await client.get(url, headers=dev.auth | {"If-None-Match": f'"{sha}"'})
    assert r.status_code == 304


async def test_asset_x_accel_redirect(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    lib = await seed_library(app, t.tenant_id, n_items=1)
    dev = await pair_device(app, client, t.tenant_id)
    app.state.asset_store = FilesystemAssetStore(
        app.state.settings.asset_dir, accel_prefix="/_assets/"
    )
    sha = lib.shas[0]
    r = await client.get(f"/api/v1/device/assets/{sha}", headers=dev.auth)
    assert r.status_code == 200
    assert r.headers["x-accel-redirect"] == f"/_assets/{sha[:2]}/{sha[2:4]}/{sha}.opus"
    assert r.headers["etag"] == f'"{sha}"'
    assert r.content == b""


async def test_asset_invalid_or_unknown(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    for sha in ("../../etc/passwd", "A" * 64, "0" * 64):
        r = await client.get(f"/api/v1/device/assets/{sha}", headers=dev.auth)
        assert r.status_code == 404


async def test_events_accept_dedupe_reject(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    ok = event("token_unknown", {"uid": "04A2B3C4D5E680"})
    batch = [
        ok,
        ok,  # duplicate inside one batch
        event("listening_minutes", {"minutes": 3}),  # not in SPEC §6.5
        event("token_unknown", {"uid": "not-hex"}),
    ]
    r = await client.post("/api/v1/device/events", json={"events": batch}, headers=dev.auth)
    assert r.status_code == 200
    results = EventBatchResponse.model_validate_json(r.content).results
    assert [x.status for x in results] == ["accepted", "duplicate", "rejected", "rejected"]
    assert results[2].code == "unknown_type"
    r = await client.post("/api/v1/device/events", json={"events": [ok]}, headers=dev.auth)
    assert EventBatchResponse.model_validate_json(r.content).results[0].status == "duplicate"
    async with sessionmaker_of(app)() as db:
        rows = (await db.scalars(select(Event))).all()
        assert len(rows) == 1
        assert rows[0].tenant_id == t.tenant_id
        assert rows[0].boot_id == uuid.UUID(BOOT)


async def test_events_batch_limit(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    batch = [event("sync_error", {"stage": "s", "code": "c"}) for _ in range(101)]
    r = await client.post("/api/v1/device/events", json={"events": batch}, headers=dev.auth)
    assert r.status_code == 400
    assert ErrorResponse.model_validate_json(r.content).error.code == ErrorCode.INVALID_REQUEST


async def test_resume_position_last_writer_wins(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    lib = await seed_library(app, t.tenant_id)
    dev = await pair_device(app, client, t.tenant_id)
    data = {"token_id": str(lib.token_id), "item_index": 1, "position_ms": 5000}
    newer = event("resume_position", data, ts="2026-09-24T18:00:00Z")
    older = event("resume_position", data | {"position_ms": 100}, ts="2026-09-24T17:00:00Z")
    await client.post("/api/v1/device/events", json={"events": [newer, older]}, headers=dev.auth)
    async with sessionmaker_of(app)() as db:
        pos = await db.get(ResumePosition, (t.tenant_id, lib.token_id))
        assert pos is not None
        assert pos.position_ms == 5000
        assert pos.updated_at == dt.datetime(2026, 9, 24, 18, tzinfo=dt.UTC)


async def test_reported_is_stored(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    body = {
        "v": 1,
        "id": ulid(),
        "ts": "2026-09-24T18:02:11Z",
        "type": "reported",
        "data": {
            "agent_version": "0.3.1",
            "hw_model": "rpi-zero2w",
            "applied_config_rev": 3,
            "applied_device_rev": 1,
            "storage": {"free_mb": 9120},
            "time_trusted": True,
            "playback": {"status": "playing", "volume": 35},
        },
    }
    r = await client.post("/api/v1/device/reported", json=body, headers=dev.auth)
    assert r.status_code == 204
    async with sessionmaker_of(app)() as db:
        device = await db.get(Device, dev.device_id)
        assert device is not None
        assert device.reported is not None
        assert device.reported["applied_config_rev"] == 3
        assert device.agent_version == "0.3.1"
        assert device.reported_at is not None


async def test_unpair(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    r = await client.post("/api/v1/device/unpair", headers=dev.auth)
    assert r.status_code == 204
    async with sessionmaker_of(app)() as db:
        device = await db.get(Device, dev.device_id)
        assert device is not None
        assert device.tenant_id is None
        assert device.secret_hash is None
        assert device.device_rev == 2  # config deleted → +1
        assert await db.get(DeviceConfig, dev.device_id) is None
    assert (await client.get("/api/v1/device/state", headers=dev.auth)).status_code == 401
    r = await client.post(
        "/api/v1/device/token", json={"device_id": str(dev.device_id), "device_secret": dev.secret}
    )
    assert r.status_code == 401


async def test_invalid_body_uses_error_format(client: httpx.AsyncClient) -> None:
    r = await client.post("/api/v1/pairing/start", json={"device_id": "nope"})
    assert r.status_code == 400
    assert ErrorResponse.model_validate_json(r.content).error.code == ErrorCode.INVALID_REQUEST
