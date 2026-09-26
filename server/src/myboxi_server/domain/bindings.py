"""Figure → content bindings, per tenant (SPEC §3.9)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_server.domain.authz import Perm, TenantContext
from myboxi_server.domain.contents import get_content
from myboxi_server.domain.errors import InvalidInputError
from myboxi_server.domain.tokens import get_token
from myboxi_server.ids import uuid7
from myboxi_server.models import Binding, ResumePosition
from myboxi_server.models.enums import RepeatMode


async def get_binding(db: AsyncSession, ctx: TenantContext, token_id: uuid.UUID) -> Binding | None:
    ctx.require(Perm.READ)
    return await db.scalar(
        select(Binding).where(Binding.token_id == token_id, Binding.tenant_id == ctx.tenant_id)
    )


async def bindings_by_token(db: AsyncSession, ctx: TenantContext) -> dict[uuid.UUID, Binding]:
    ctx.require(Perm.READ)
    rows = await db.scalars(select(Binding).where(Binding.tenant_id == ctx.tenant_id))
    return {b.token_id: b for b in rows}


async def set_binding(
    db: AsyncSession,
    ctx: TenantContext,
    token_id: uuid.UUID,
    *,
    content_id: uuid.UUID,
    resume: bool,
    shuffle: bool,
    repeat: RepeatMode,
) -> Binding:
    """One figure → exactly one content (SPEC §3.9)."""
    ctx.require(Perm.BINDING_WRITE)
    await get_token(db, ctx, token_id)
    await get_content(db, ctx, content_id)
    binding = await get_binding(db, ctx, token_id)
    if binding is None:
        binding = Binding(tenant_id=ctx.tenant_id, token_id=token_id, content_id=content_id)
        db.add(binding)
    if binding.content_id != content_id:
        binding.start_at = None  # a start point belongs to the content it was chosen for
    binding.content_id = content_id
    binding.resume = resume
    binding.shuffle = shuffle
    binding.repeat = repeat
    await db.flush()
    return binding


async def delete_binding(db: AsyncSession, ctx: TenantContext, token_id: uuid.UUID) -> None:
    ctx.require(Perm.BINDING_WRITE)
    await get_token(db, ctx, token_id)
    binding = await get_binding(db, ctx, token_id)
    if binding is not None:
        await db.delete(binding)
        await db.flush()


async def set_start(
    db: AsyncSession,
    ctx: TenantContext,
    token_id: uuid.UUID,
    item_index: int,
    *,
    now: dt.datetime | None = None,
) -> Binding:
    """SPEC v0.11 §3.9: the next placement starts here, once ("von vorn", "ab Titel 4")."""
    ctx.require(Perm.BINDING_WRITE)
    binding = await get_binding(db, ctx, token_id)
    if binding is None:
        raise InvalidInputError("Die Figur hat noch keinen Inhalt.")
    if not 0 <= item_index <= 10_000:
        raise InvalidInputError("Diesen Titel gibt es nicht.")
    binding.start_at = {
        "id": str(uuid7()),
        "item_index": item_index,
        "position_ms": 0,
        "set_at": (now or dt.datetime.now(dt.UTC)).isoformat(),
    }
    await db.flush()
    return binding


@dataclass(frozen=True)
class Listening:
    """What the figure page shows: the last reported position and a waiting start point."""

    position: ResumePosition | None
    pending_index: int | None


async def listening(db: AsyncSession, ctx: TenantContext, token_id: uuid.UUID) -> Listening:
    ctx.require(Perm.READ)
    position = await db.get(ResumePosition, (ctx.tenant_id, token_id))
    binding = await get_binding(db, ctx, token_id)
    pending: int | None = None
    start = binding.start_at if binding is not None else None
    if start is not None:
        set_at = dt.datetime.fromisoformat(str(start.get("set_at")))
        # Applied once the box reports a position from after the start point was set.
        if position is None or position.updated_at < set_at:
            pending = int(start.get("item_index", 0))
    return Listening(position, pending)
