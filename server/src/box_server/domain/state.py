"""Device state snapshot (SPEC §5.4). M1 always answers with ``full: true``."""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import TypeAdapter
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from box_protocol.state import (
    BindingUpsert,
    ContentItemUpsert,
    ContentUpsert,
    QuietHours,
    StateResponse,
    TokenUpsert,
    Upserts,
)
from box_protocol.state import (
    DeviceConfig as DeviceConfigMsg,
)
from box_server.models import (
    Asset,
    Binding,
    Content,
    ContentItem,
    Device,
    DeviceConfig,
    Tenant,
    Token,
)

_content_adapter: TypeAdapter[ContentUpsert] = TypeAdapter(ContentUpsert)


def device_config_message(cfg: DeviceConfig | None) -> DeviceConfigMsg:
    if cfg is None:
        return DeviceConfigMsg()
    return DeviceConfigMsg.model_validate(
        {
            "max_volume": cfg.max_volume,
            "start_volume": cfg.start_volume,
            "quiet_hours": QuietHours.model_validate(cfg.quiet_hours) if cfg.quiet_hours else None,
            "sleep_timer_min": cfg.sleep_timer_min,
            "on_token_removed": cfg.on_token_removed.value,
            "locale": cfg.locale,
            "timezone": cfg.timezone,
            "providers_enabled": cfg.providers_enabled,
        }
    )


def content_message(content: Content) -> ContentUpsert:
    data: dict[str, Any] = {
        "id": content.id,
        "kind": content.kind.value,
        "title": content.title,
        "rev": content.rev,
        "source": content.source,
    }
    return _content_adapter.validate_python(data)


async def _snapshot(db: AsyncSession, tenant_id: uuid.UUID, device_id: uuid.UUID) -> StateResponse:
    config_rev = (
        await db.execute(select(Tenant.config_rev).where(Tenant.id == tenant_id))
    ).scalar_one()
    device_rev = (
        await db.execute(
            select(Device.device_rev).where(Device.id == device_id, Device.tenant_id == tenant_id)
        )
    ).scalar_one()
    cfg = await db.scalar(
        select(DeviceConfig).where(
            DeviceConfig.device_id == device_id, DeviceConfig.tenant_id == tenant_id
        )
    )
    tokens = await db.scalars(select(Token).where(Token.tenant_id == tenant_id).order_by(Token.id))
    contents = await db.scalars(
        select(Content).where(Content.tenant_id == tenant_id).order_by(Content.id)
    )
    items = await db.execute(
        select(ContentItem, Asset.sha256, Asset.bytes)
        .join(
            Asset, (Asset.id == ContentItem.asset_id) & (Asset.tenant_id == ContentItem.tenant_id)
        )
        .where(ContentItem.tenant_id == tenant_id)
        .order_by(ContentItem.content_id, ContentItem.position)
    )
    bindings = await db.scalars(
        select(Binding).where(Binding.tenant_id == tenant_id).order_by(Binding.token_id)
    )
    return StateResponse(
        full=True,
        config_rev=config_rev,
        device_rev=device_rev,
        upserts=Upserts(
            token=[TokenUpsert(id=t.id, uid=t.uid, label=t.label) for t in tokens],
            content=[content_message(c) for c in contents],
            content_item=[
                ContentItemUpsert(
                    content_id=item.content_id,
                    position=item.position,
                    asset_sha256=sha,
                    bytes=size,
                    title=item.title,
                    duration_ms=item.duration_ms,
                )
                for item, sha, size in items.tuples()
            ],
            binding=[
                BindingUpsert(
                    token_id=b.token_id,
                    content_id=b.content_id,
                    resume=b.resume,
                    shuffle=b.shuffle,
                    repeat=b.repeat.value,
                )
                for b in bindings
            ],
        ),
        device_config=device_config_message(cfg),
    )


async def snapshot(
    engine: AsyncEngine, tenant_id: uuid.UUID, device_id: uuid.UUID
) -> StateResponse:
    """Revisions and data from one consistent REPEATABLE READ snapshot (SPEC §5.2)."""
    async with engine.connect() as conn:
        conn = await conn.execution_options(isolation_level="REPEATABLE READ")
        async with conn.begin():
            await conn.execute(text("SET TRANSACTION READ ONLY"))
            async with AsyncSession(bind=conn, expire_on_commit=False) as db:
                return await _snapshot(db, tenant_id, device_id)
