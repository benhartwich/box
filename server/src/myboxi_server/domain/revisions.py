"""Revisions (SPEC §5.1).

Revisions are raised by database triggers (migration 0003), never by application code:

* ``tenant.config_rev`` +1 once per transaction on any change to token, content,
  content_item or binding of the tenant,
* ``content.rev`` +1 once per transaction on any change to the content or its items,
* ``device.device_rev`` +1 once per transaction on any change to the device's device_config.

This covers every write path (web, jobs, CLI, cascades) without per-endpoint code.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_server.models import Device, Tenant


async def tenant_config_rev(session: AsyncSession, tenant_id: uuid.UUID) -> int:
    rev = await session.scalar(select(Tenant.config_rev).where(Tenant.id == tenant_id))
    if rev is None:
        raise LookupError(tenant_id)
    return rev


async def device_rev(session: AsyncSession, device_id: uuid.UUID) -> int:
    rev = await session.scalar(select(Device.device_rev).where(Device.id == device_id))
    if rev is None:
        raise LookupError(device_id)
    return rev
