"""Boxes of a tenant (SPEC §3.3, §3.4)."""

from __future__ import annotations

import datetime as dt
import uuid
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_protocol.reported import ReportedData
from myboxi_protocol.state import DeviceConfig as DeviceConfigMsg
from myboxi_server.domain.authz import Perm, TenantContext
from myboxi_server.domain.errors import InvalidInputError, NotFoundError
from myboxi_server.domain.state import device_config_message
from myboxi_server.models import Device, DeviceConfig
from myboxi_server.models.enums import OnTokenRemoved


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
    if device.mqtt_provisioned or device.mqtt_password is not None:
        device.mqtt_revoke = True  # the MQTT service deletes the broker account (SPEC §6)
    device.mqtt_password = None
    device.tenant_id = None
    device.secret_hash = None
    device.name = ""
    device.reported = None
    device.reported_at = None
    device.auth_generation += 1
    await db.flush()


def store_reported(device: Device, data: ReportedData, now: dt.datetime) -> None:
    """SPEC §6.4: over MQTT or ``POST /device/reported``; the server only stores it."""
    device.reported = data.model_dump(mode="json")
    device.reported_at = now
    device.last_seen_at = now
    device.agent_version = data.agent_version
    device.hw_model = data.hw_model


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


async def rename_device(
    db: AsyncSession, ctx: TenantContext, device_id: uuid.UUID, name: str
) -> None:
    ctx.require(Perm.DEVICE_RENAME)
    name = name.strip()
    if not 1 <= len(name) <= 64:
        raise InvalidInputError("Bitte einen Namen mit höchstens 64 Zeichen angeben.")
    device = await get_device(db, ctx, device_id, for_update=True)
    device.name = name
    await db.flush()


async def _config_row(
    db: AsyncSession, ctx: TenantContext, device_id: uuid.UUID
) -> DeviceConfig | None:
    return await db.scalar(
        select(DeviceConfig).where(
            DeviceConfig.device_id == device_id, DeviceConfig.tenant_id == ctx.tenant_id
        )
    )


async def get_config(db: AsyncSession, ctx: TenantContext, device_id: uuid.UUID) -> DeviceConfigMsg:
    """The box's desired configuration as sent in the state (SPEC §3.4 defaults if unset)."""
    ctx.require(Perm.READ)
    await get_device(db, ctx, device_id)
    return device_config_message(await _config_row(db, ctx, device_id))


async def update_config(
    db: AsyncSession, ctx: TenantContext, device_id: uuid.UUID, config: DeviceConfigMsg
) -> DeviceConfig:
    """SPEC §3.4, validated with the protocol model. Raises device_rev via trigger (§5.1)."""
    ctx.require(Perm.DEVICE_CONFIG)
    try:
        ZoneInfo(config.timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise InvalidInputError("Unbekannte Zeitzone.") from None
    await get_device(db, ctx, device_id, for_update=True)
    cfg = await _config_row(db, ctx, device_id)
    if cfg is None:
        cfg = DeviceConfig(device_id=device_id, tenant_id=ctx.tenant_id)
        db.add(cfg)
    cfg.max_volume = config.max_volume
    cfg.start_volume = config.start_volume
    cfg.quiet_hours = (
        config.quiet_hours.model_dump(mode="json", exclude_none=True)
        if config.quiet_hours
        else None
    )
    cfg.sleep_timer_min = config.sleep_timer_min
    cfg.on_token_removed = OnTokenRemoved(config.on_token_removed)
    cfg.locale = config.locale
    cfg.timezone = config.timezone
    cfg.providers_enabled = list(dict.fromkeys(config.providers_enabled))
    cfg.auto_update = config.auto_update
    cfg.spotify_allow_explicit = config.spotify_allow_explicit
    await db.flush()
    return cfg
