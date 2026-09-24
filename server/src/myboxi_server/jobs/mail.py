"""Mail jobs."""

from __future__ import annotations

import asyncio
import logging
import uuid

from myboxi_server.auth.mail import invitation_mail, send_smtp
from myboxi_server.domain import members
from myboxi_server.jobs.app import job_app
from myboxi_server.jobs.context import job_sessionmaker, job_settings
from myboxi_server.models import Invitation, Tenant

log = logging.getLogger(__name__)


@job_app.task(name="send_invitation_mail", queue="mail", retry=5)
async def send_invitation_mail(invitation_id: str) -> None:
    """Rotate the invitation token and mail the new link (no plaintext token in the queue)."""
    settings = job_settings()
    async with job_sessionmaker()() as db:
        inv = await db.get(Invitation, uuid.UUID(invitation_id))
        if inv is None:
            return
        tenant = await db.get(Tenant, inv.tenant_id)
        token = await members.rotate_invitation_token(db, inv.id)
        if token is None:
            log.info("invitation no longer open", extra={"invitation_id": invitation_id})
            return
        await db.commit()
        link = f"{settings.base_url.rstrip('/')}/invite?token={token}"
        mail = invitation_mail(inv.email, tenant.name if tenant else "", link)
    await asyncio.to_thread(send_smtp, settings, mail)
