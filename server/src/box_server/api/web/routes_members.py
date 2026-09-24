"""Tenant home and member management (SPEC §3.2: only owners invite and manage)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from box_server.api.web.deps import CurrentSession, DbSession, SettingsDep, csrf_protect, require
from box_server.api.web.render import render
from box_server.auth.mail import invitation_mail, log_mail
from box_server.domain import members
from box_server.domain.authz import Perm, TenantContext
from box_server.domain.errors import DomainError, NotFoundError
from box_server.jobs.mail import send_invitation_mail
from box_server.models import Tenant
from box_server.models.enums import Role

router = APIRouter(prefix="/t/{tid}", dependencies=[Depends(csrf_protect)])

ReadCtx = Annotated[TenantContext, Depends(require(Perm.READ))]
InviteCtx = Annotated[TenantContext, Depends(require(Perm.MEMBER_INVITE))]
ManageCtx = Annotated[TenantContext, Depends(require(Perm.MEMBER_MANAGE))]


@router.get("/")
async def tenant_home(
    request: Request, db: DbSession, session: CurrentSession, ctx: ReadCtx
) -> Response:
    tenant = await db.get(Tenant, ctx.tenant_id)
    return render(request, "home.html", {"tenant": tenant}, session=session, ctx=ctx)


async def _members_page(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: TenantContext,
    *,
    error: str | None = None,
    invite_link: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    invitations = (
        await members.list_open_invitations(db, ctx) if ctx.can(Perm.MEMBER_INVITE) else []
    )
    return render(
        request,
        "members.html",
        {
            "members": await members.list_members(db, ctx),
            "invitations": invitations,
            "roles": list(Role),
            "error": error,
            "invite_link": invite_link,
            "notice": notice,
        },
        session=session,
        ctx=ctx,
        status_code=status_code,
    )


@router.get("/members")
async def members_page(
    request: Request, db: DbSession, session: CurrentSession, ctx: ReadCtx
) -> Response:
    return await _members_page(request, db, session, ctx)


@router.post("/members/invite")
async def invite(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    session: CurrentSession,
    ctx: InviteCtx,
    email: Annotated[str, Form(max_length=254)],
    role: Annotated[Role, Form()],
) -> Response:
    try:
        invitation, token = await members.create_invitation(db, ctx, email, role)
    except DomainError as exc:
        return await _members_page(request, db, session, ctx, error=exc.message, status_code=400)
    await db.commit()
    tenant = await db.get(Tenant, ctx.tenant_id)
    if settings.mail_backend == "smtp":
        # The job rotates the token, so no usable link sits in the job queue.
        await request.app.state.job_app.configure_task(send_invitation_mail.name).defer_async(
            invitation_id=str(invitation.id)
        )
        return await _members_page(
            request, db, session, ctx, notice=f"Einladung an {invitation.email} wird verschickt."
        )
    link = f"{settings.base_url.rstrip('/')}/invite?token={token}"
    log_mail(settings, invitation_mail(invitation.email, tenant.name if tenant else "", link))
    # Without SMTP the owner passes the link on personally; it is shown exactly once.
    return await _members_page(request, db, session, ctx, invite_link=link)


@router.post("/members/invitations/{invitation_id}/revoke")
async def revoke(db: DbSession, ctx: InviteCtx, invitation_id: uuid.UUID) -> Response:
    await members.revoke_invitation(db, ctx, invitation_id)
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/members", status_code=303)


@router.post("/members/{user_id}/role")
async def change_role(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ManageCtx,
    user_id: uuid.UUID,
    role: Annotated[Role, Form()],
) -> Response:
    try:
        await members.change_role(db, ctx, user_id, role)
    except NotFoundError:
        raise
    except DomainError as exc:
        await db.rollback()
        return await _members_page(request, db, session, ctx, error=exc.message, status_code=400)
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/members", status_code=303)


@router.post("/members/{user_id}/remove")
async def remove(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ManageCtx,
    user_id: uuid.UUID,
) -> Response:
    try:
        await members.remove_member(db, ctx, user_id)
    except NotFoundError:
        raise
    except DomainError as exc:
        await db.rollback()
        return await _members_page(request, db, session, ctx, error=exc.message, status_code=400)
    await db.commit()
    target = "/" if user_id == session.user.id else f"/t/{ctx.tenant_id}/members"
    return RedirectResponse(target, status_code=303)
