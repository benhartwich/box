"""Boxes of a tenant (SPEC §3.3, §3.4)."""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from box_server.domain.authz import Perm, TenantContext
from box_server.domain.errors import NotFoundError
from box_server.models import Device, DeviceConfig


async def unpair(db: AsyncSession, device: Device) -> None:
    """SPEC §7.3 ``/device/unpair`` and removal in the app. The caller commits.

    The device row stays (its id belongs to the box); config and credentials go.
    """
    if device.tenant_id is not None:
        await db.execute(
            delete(DeviceConfig).where(
                DeviceConfig.device_id == device.id, DeviceConfig.tenant_id == device.tenant_id
            )
        )
    device.tenant_id = None
    device.secret_hash = None
    device.name = ""
    device.reported = None
    device.reported_at = None
    device.auth_generation += 1
    await db.flush()


async def list_devices(db: AsyncSession, ctx: TenantContext) -> list[Device]:
    ctx.require(Perm.READ)
    return list(
        await db.scalars(
            select(Device).where(Device.tenant_id == ctx.tenant_id).order_by(Device.name)
        )
    )


async def get_device(
    db: AsyncSession, ctx: TenantContext, device_id: uuid.UUID, *, for_update: bool = False
) -> Device:
    ctx.require(Perm.READ)
    stmt = select(Device).where(Device.id == device_id, Device.tenant_id == ctx.tenant_id)
    if for_update:
        stmt = stmt.with_for_update()
    device = await db.scalar(stmt)
    if device is None:
        raise NotFoundError()
    return device


async def remove_device(db: AsyncSession, ctx: TenantContext, device_id: uuid.UUID) -> None:
    ctx.require(Perm.DEVICE_REMOVE)
    await unpair(db, await get_device(db, ctx, device_id, for_update=True))
