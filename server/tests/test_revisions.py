"""SPEC §5.1: revisions rise exactly once per transaction (database triggers, migration 0003)."""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

import pytest
from sqlalchemy import delete, insert, select, text, update
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from myboxi_server.models import (
    Asset,
    Binding,
    Content,
    ContentItem,
    Device,
    DeviceConfig,
    ResumePosition,
    Tenant,
    Token,
)


@dataclass
class World:
    tenant: uuid.UUID
    other: uuid.UUID
    token: uuid.UUID
    content: uuid.UUID
    asset: uuid.UUID
    device: uuid.UUID


@pytest.fixture
async def world(engine: AsyncEngine) -> World:
    w = World(*(uuid.uuid4() for _ in range(6)))
    async with engine.begin() as c:
        await c.execute(
            insert(Tenant), [{"id": w.tenant, "name": "A"}, {"id": w.other, "name": "B"}]
        )
        await c.execute(
            insert(Token).values(id=w.token, tenant_id=w.tenant, uid="04A2B3C4", label="Bibi")
        )
        await c.execute(
            insert(Asset).values(
                id=w.asset,
                tenant_id=w.tenant,
                sha256="a" * 64,
                mime="audio/ogg",
                bytes=1,
                storage_path="aa/aa/a.opus",
            )
        )
        await c.execute(
            insert(Content).values(id=w.content, tenant_id=w.tenant, kind="collection", title="C")
        )
        await c.execute(
            insert(Device).values(
                id=w.device, tenant_id=w.tenant, hw_model="rpi4", agent_version="0"
            )
        )
    return w


async def config_rev(engine: AsyncEngine, tenant: uuid.UUID) -> int:
    async with engine.connect() as c:
        return (await c.execute(select(Tenant.config_rev).where(Tenant.id == tenant))).scalar_one()


async def content_rev(engine: AsyncEngine, content: uuid.UUID) -> int:
    async with engine.connect() as c:
        return (await c.execute(select(Content.rev).where(Content.id == content))).scalar_one()


async def device_rev(engine: AsyncEngine, device: uuid.UUID) -> int:
    async with engine.connect() as c:
        return (await c.execute(select(Device.device_rev).where(Device.id == device))).scalar_one()


Op = Callable[[AsyncConnection, World], Awaitable[object]]


def _item(w: World, position: int) -> dict[str, object]:
    return {
        "tenant_id": w.tenant,
        "content_id": w.content,
        "position": position,
        "asset_id": w.asset,
        "title": f"t{position}",
        "duration_ms": 1000,
    }


OPS: dict[str, Op] = {
    "token_insert": lambda c, w: c.execute(
        insert(Token).values(tenant_id=w.tenant, uid="04000001", label="x")
    ),
    "token_insert_many": lambda c, w: c.execute(
        insert(Token),
        [{"tenant_id": w.tenant, "uid": f"0400000{i}", "label": "x"} for i in range(3)],
    ),
    "token_update": lambda c, w: c.execute(
        update(Token).where(Token.id == w.token).values(label="y")
    ),
    "token_delete": lambda c, w: c.execute(delete(Token).where(Token.id == w.token)),
    "content_insert": lambda c, w: c.execute(
        insert(Content).values(
            tenant_id=w.tenant, kind="podcast", title="P", source={"feed_url": "https://x"}
        )
    ),
    "content_update": lambda c, w: c.execute(
        update(Content).where(Content.id == w.content).values(title="D")
    ),
    "content_delete": lambda c, w: c.execute(delete(Content).where(Content.id == w.content)),
    "items_insert_many": lambda c, w: c.execute(
        insert(ContentItem), [_item(w, i) for i in range(3)]
    ),
    "binding_insert": lambda c, w: c.execute(
        insert(Binding).values(tenant_id=w.tenant, token_id=w.token, content_id=w.content)
    ),
}


@pytest.mark.parametrize("op", OPS.keys())
async def test_each_write_raises_config_rev_once(
    engine: AsyncEngine, world: World, op: str
) -> None:
    before = await config_rev(engine, world.tenant)
    other_before = await config_rev(engine, world.other)
    async with engine.begin() as c:
        await OPS[op](c, world)
    assert await config_rev(engine, world.tenant) == before + 1
    assert await config_rev(engine, world.other) == other_before


async def test_many_writes_in_one_transaction_raise_once(engine: AsyncEngine, world: World) -> None:
    before = await config_rev(engine, world.tenant)
    async with engine.begin() as c:
        for op in (
            "token_insert",
            "token_update",
            "content_update",
            "items_insert_many",
            "binding_insert",
        ):
            await OPS[op](c, world)
    assert await config_rev(engine, world.tenant) == before + 1


async def test_separate_transactions_raise_each(engine: AsyncEngine, world: World) -> None:
    before = await config_rev(engine, world.tenant)
    for label in ("a", "b", "c"):
        async with engine.begin() as c:
            await c.execute(update(Token).where(Token.id == world.token).values(label=label))
    assert await config_rev(engine, world.tenant) == before + 3


async def test_rollback_raises_nothing(engine: AsyncEngine, world: World) -> None:
    before = await config_rev(engine, world.tenant)
    async with engine.connect() as c:
        trans = await c.begin()
        await OPS["token_insert"](c, world)
        await trans.rollback()
    assert await config_rev(engine, world.tenant) == before


async def test_binding_update_and_delete(engine: AsyncEngine, world: World) -> None:
    async with engine.begin() as c:
        await OPS["binding_insert"](c, world)
    before = await config_rev(engine, world.tenant)
    async with engine.begin() as c:
        await c.execute(update(Binding).where(Binding.token_id == world.token).values(shuffle=True))
    async with engine.begin() as c:
        await c.execute(delete(Binding).where(Binding.token_id == world.token))
    assert await config_rev(engine, world.tenant) == before + 2


async def test_cascade_delete_raises_once(engine: AsyncEngine, world: World) -> None:
    async with engine.begin() as c:
        await OPS["items_insert_many"](c, world)
        await OPS["binding_insert"](c, world)
    before = await config_rev(engine, world.tenant)
    async with engine.begin() as c:
        await OPS["content_delete"](c, world)
    assert await config_rev(engine, world.tenant) == before + 1


async def test_non_config_writes_do_not_raise(engine: AsyncEngine, world: World) -> None:
    before = await config_rev(engine, world.tenant)
    async with engine.begin() as c:
        await c.execute(update(Tenant).where(Tenant.id == world.tenant).values(name="Renamed"))
        await c.execute(
            insert(Asset).values(
                tenant_id=world.tenant,
                sha256="c" * 64,
                mime="audio/ogg",
                bytes=2,
                storage_path="cc/cc/c.opus",
            )
        )
        await c.execute(
            insert(ResumePosition).values(
                tenant_id=world.tenant,
                token_id=world.token,
                item_index=0,
                position_ms=5,
                updated_at=dt.datetime.now(dt.UTC),
            )
        )
        await c.execute(update(Device).where(Device.id == world.device).values(name="Kinderzimmer"))
    assert await config_rev(engine, world.tenant) == before


async def test_content_rev_new_content_with_items_is_rev_1(
    engine: AsyncEngine, world: World
) -> None:
    new = uuid.uuid4()
    async with engine.begin() as c:
        await c.execute(
            insert(Content).values(id=new, tenant_id=world.tenant, kind="collection", title="N")
        )
        await c.execute(
            insert(ContentItem),
            [{**_item(world, i), "content_id": new} for i in range(3)],
        )
    assert await content_rev(engine, new) == 1


async def test_content_rev_raises_once_per_transaction(engine: AsyncEngine, world: World) -> None:
    assert await content_rev(engine, world.content) == 1
    async with engine.begin() as c:
        await OPS["items_insert_many"](c, world)
        await OPS["content_update"](c, world)
    assert await content_rev(engine, world.content) == 2
    # Reorder: swap positions 0 and 2 (deferred unique constraint).
    async with engine.begin() as c:
        await c.execute(
            text(
                "UPDATE content_item SET position = CASE position WHEN 0 THEN 2 WHEN 2 THEN 0 END "
                "WHERE content_id = :cid AND position IN (0, 2)"
            ),
            {"cid": world.content},
        )
    assert await content_rev(engine, world.content) == 3
    async with engine.begin() as c:
        await c.execute(
            delete(ContentItem).where(
                ContentItem.content_id == world.content, ContentItem.position == 1
            )
        )
    assert await content_rev(engine, world.content) == 4


async def test_device_rev(engine: AsyncEngine, world: World) -> None:
    cfg_before = await config_rev(engine, world.tenant)
    assert await device_rev(engine, world.device) == 0
    async with engine.begin() as c:
        await c.execute(insert(DeviceConfig).values(device_id=world.device, tenant_id=world.tenant))
    assert await device_rev(engine, world.device) == 1
    async with engine.begin() as c:
        await c.execute(
            update(DeviceConfig).where(DeviceConfig.device_id == world.device).values(max_volume=50)
        )
        await c.execute(
            update(DeviceConfig)
            .where(DeviceConfig.device_id == world.device)
            .values(start_volume=20)
        )
    assert await device_rev(engine, world.device) == 2
    async with engine.begin() as c:
        await c.execute(delete(DeviceConfig).where(DeviceConfig.device_id == world.device))
    assert await device_rev(engine, world.device) == 3
    assert await config_rev(engine, world.tenant) == cfg_before
