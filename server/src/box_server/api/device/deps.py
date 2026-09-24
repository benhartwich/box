"""Bearer authentication for device endpoints (SPEC §7.2)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Request
from sqlalchemy import select, update

from box_server.api.device.errors import unauthorized
from box_server.api.device.jwt import InvalidTokenError, verify
from box_server.api.web.deps import DbSession, SettingsDep
from box_server.models import Device

LAST_SEEN_RESOLUTION = dt.timedelta(seconds=60)


@dataclass(frozen=True)
class DeviceContext:
    """An authenticated, currently paired box. Everything it may see is scoped by ``tenant_id``."""

    device_id: uuid.UUID
    tenant_id: uuid.UUID


async def current_device(request: Request, db: DbSession, settings: SettingsDep) -> DeviceContext:
    auth = request.headers.get("authorization", "")
    scheme, _, token = auth.partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise unauthorized("Missing bearer token")
    try:
        claims = verify(settings.device_jwt_key.get_secret_value(), token.strip())
    except InvalidTokenError:
        raise unauthorized("Invalid token") from None
    # Unpair or re-pairing bumps auth_generation, so older tokens stop working at once.
    row = (
        await db.execute(
            select(Device.last_seen_at).where(
                Device.id == claims.device_id,
                Device.tenant_id == claims.tenant_id,
                Device.auth_generation == claims.generation,
            )
        )
    ).one_or_none()
    if row is None:
        raise unauthorized("Device is not paired")
    now = dt.datetime.now(dt.UTC)
    last_seen: dt.datetime | None = row[0]
    if last_seen is None or now - last_seen > LAST_SEEN_RESOLUTION:
        await db.execute(
            update(Device).where(Device.id == claims.device_id).values(last_seen_at=now)
        )
        await db.commit()
    return DeviceContext(claims.device_id, claims.tenant_id)


CurrentDevice = Annotated[DeviceContext, Depends(current_device)]
