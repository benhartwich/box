"""Remote commands from the app (SPEC §6.2, M2); ``myboxi-server mqtt`` sends them."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import TypeAdapter, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_protocol.messages import DEFAULT_CMD_TTL_S, CmdData
from myboxi_server.domain.authz import Perm, TenantContext
from myboxi_server.domain.devices import get_device
from myboxi_server.domain.errors import InvalidInputError
from myboxi_server.domain.tokens import get_token
from myboxi_server.ids import ulid
from myboxi_server.models import DeviceCommand

_CMD: TypeAdapter[CmdData] = TypeAdapter(CmdData)


async def send_command(
    db: AsyncSession,
    ctx: TenantContext,
    device_id: uuid.UUID,
    name: str,
    args: dict[str, Any] | None = None,
    *,
    now: dt.datetime | None = None,
) -> DeviceCommand:
    """Validated with the protocol model; expires after 60 s (SPEC §6.2). The caller commits."""
    ctx.require(Perm.DEVICE_CONFIG)
    device = await get_device(db, ctx, device_id)
    if not device.mqtt_provisioned:
        raise InvalidInputError("Diese Box ist nicht mit dem Nachrichtendienst verbunden.")
    now = now or dt.datetime.now(dt.UTC)
    expires = now + dt.timedelta(seconds=DEFAULT_CMD_TTL_S)
    try:
        cmd = _CMD.validate_python({"name": name, "args": args or {}, "expires_at": expires})
    except ValidationError:
        raise InvalidInputError("Diesen Befehl kennt die Box nicht.") from None
    if cmd.name == "play_token":
        await get_token(db, ctx, cmd.args.token_id)  # a figure of this household only
    row = DeviceCommand(
        id=ulid(),
        tenant_id=ctx.tenant_id,
        device_id=device_id,
        name=cmd.name,
        args=cmd.args.model_dump(mode="json"),
        expires_at=expires,
    )
    db.add(row)
    await db.flush()
    return row


async def recent_commands(
    db: AsyncSession, ctx: TenantContext, device_id: uuid.UUID, limit: int = 5
) -> list[DeviceCommand]:
    ctx.require(Perm.READ)
    return list(
        await db.scalars(
            select(DeviceCommand)
            .where(DeviceCommand.tenant_id == ctx.tenant_id, DeviceCommand.device_id == device_id)
            .order_by(DeviceCommand.created_at.desc())
            .limit(limit)
        )
    )
