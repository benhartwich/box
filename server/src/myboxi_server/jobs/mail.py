"""Mail jobs."""

from __future__ import annotations

import asyncio
import logging
import uuid

from myboxi_server.auth.mail import Mail, invitation_mail, send_smtp
from myboxi_server.domain import case_requests, members
from myboxi_server.domain.case_mails import confirm_mail, notify_mails
from myboxi_server.jobs.app import job_app
from myboxi_server.jobs.context import job_sessionmaker, job_settings
from myboxi_server.models import CaseRequest, Invitation, Tenant

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


@job_app.task(name="send_case_confirmation", queue="mail", retry=5)
async def send_case_confirmation(request_id: str) -> None:
    """Double opt-in for an order request; the token is rotated here, never queued."""
    settings = job_settings()
    async with job_sessionmaker()() as db:
        token = await case_requests.rotate_token(db, uuid.UUID(request_id))
        req = await db.get(CaseRequest, uuid.UUID(request_id))
        if token is None or req is None:
            return
        await db.commit()
        mail = confirm_mail(settings, req, token)
    await asyncio.to_thread(send_smtp, settings, mail)


@job_app.task(name="send_case_notification", queue="mail", retry=5)
async def send_case_notification(request_id: str) -> None:
    """A confirmed request: mail to the operator (reply goes to the requester) and a receipt."""
    settings = job_settings()
    async with job_sessionmaker()() as db:
        req = await db.get(CaseRequest, uuid.UUID(request_id))
        if req is None:
            return
        mails: list[Mail] = notify_mails(settings, req)
    for mail in mails:
        await asyncio.to_thread(send_smtp, settings, mail)
