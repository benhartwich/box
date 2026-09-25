"""Setup wizard: from the image on the SD card to the first figure played (SPEC v0.6)."""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from fastapi.responses import Response

from myboxi_server.api.web.deps import CurrentSession, DbSession, ReadCtx, SettingsDep, csrf_protect
from myboxi_server.api.web.render import render
from myboxi_server.auth.sessions import SessionInfo
from myboxi_server.domain import devices
from myboxi_server.domain.authz import TenantContext
from myboxi_server.domain.setup import SetupProgress, setup_progress
from myboxi_server.settings import Settings

router = APIRouter(prefix="/t/{tid}", dependencies=[Depends(csrf_protect)])

DEFAULT_SERVER = "https://app.myboxi.eu"  # SPEC §9.3: preset on the box's setup page


async def unfinished(db: DbSession, ctx: TenantContext) -> list[SetupProgress]:
    """Boxes of the household whose setup is not complete (home page, wizard start)."""
    result: list[SetupProgress] = []
    for device in await devices.list_devices(db, ctx):
        progress = await setup_progress(db, ctx, device)
        if not progress.done:
            result.append(progress)
    return result


async def render_start(
    request: Request,
    db: DbSession,
    session: SessionInfo,
    ctx: TenantContext,
    settings: Settings,
    *,
    error: str | None = None,
    form: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    server = settings.base_url.rstrip("/")
    return render(
        request,
        "setup.html",
        {
            "unfinished": await unfinished(db, ctx),
            "image_url": settings.image_url,
            "docs_url": settings.docs_url.rstrip("/"),
            "server_url": server,
            "custom_server": server != DEFAULT_SERVER,
            "form": form or {},
            "claim_error": error,
        },
        session=session,
        ctx=ctx,
        status_code=status_code,
    )


@router.get("/setup")
async def setup_start(
    request: Request, db: DbSession, session: CurrentSession, ctx: ReadCtx, settings: SettingsDep
) -> Response:
    return await render_start(request, db, session, ctx, settings)


@router.get("/boxes/{device_id}/setup")
async def box_setup(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ReadCtx,
    settings: SettingsDep,
    device_id: uuid.UUID,
) -> Response:
    device = await devices.get_device(db, ctx, device_id)
    return render(
        request,
        "box_setup.html",
        {
            "device": device,
            "progress": await setup_progress(db, ctx, device),
            "docs_url": settings.docs_url.rstrip("/"),
        },
        session=session,
        ctx=ctx,
    )


@router.get("/boxes/{device_id}/setup/status")
async def box_setup_status(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ReadCtx,
    device_id: uuid.UUID,
    v: str = "",
) -> Response:
    """HTMX fragment, polled every 3 s. Unchanged state: 204, so htmx keeps the page (and
    whatever someone is typing into a form in it)."""
    device = await devices.get_device(db, ctx, device_id)
    progress = await setup_progress(db, ctx, device)
    if v and v == progress.key:
        return Response(status_code=204)
    return render(
        request,
        "_setup_steps.html",
        {"device": device, "progress": progress},
        session=session,
        ctx=ctx,
    )
