"""Figure → content bindings, per tenant (SPEC §3.9)."""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_server.domain.authz import Perm, TenantContext
from myboxi_server.domain.contents import get_content
from myboxi_server.domain.tokens import get_token
from myboxi_server.models import Binding
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
