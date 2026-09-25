"""Boxes: add by pairing code, rename, device_config, reported state (SPEC §3.3, §3.4, §6.4)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from myboxi_protocol.errors import ErrorCode
from myboxi_protocol.reported import ReportedData
from myboxi_protocol.state import DeviceConfig as DeviceConfigMsg
from myboxi_server.api.device.claim import claim_with_limits
from myboxi_server.api.device.errors import ApiError
from myboxi_server.api.web.deps import (
    ClaimCtx,
    ConfigCtx,
    CurrentSession,
    DbSession,
    ReadCtx,
    RemoveCtx,
    RenameCtx,
    SettingsDep,
    csrf_protect,
)
from myboxi_server.api.web.render import render
from myboxi_server.api.web.routes_setup import render_start
from myboxi_server.auth.sessions import SessionInfo
from myboxi_server.domain import devices, events
from myboxi_server.domain.authz import TenantContext
from myboxi_server.domain.errors import DomainError, NotFoundError
from myboxi_server.domain.setup import health_hints
from myboxi_server.domain.updates import Release, software_view
from myboxi_server.models import Device, Tenant

router = APIRouter(prefix="/t/{tid}/boxes", dependencies=[Depends(csrf_protect)])

PROVIDERS = [
    ("local", "Eigene Dateien"),
    ("podcast", "Podcasts"),
    ("spotify", "Spotify"),
    ("stream", "Streams"),
]
LOCALES = ["de-AT", "de-DE", "de-CH", "en-GB", "en-US"]
TIMEZONES = ["Europe/Vienna", "Europe/Berlin", "Europe/Zurich", "Europe/London", "UTC"]
SOLOIST_WARN = dt.timedelta(days=14)
PROVIDER_NAMES = dict(PROVIDERS)
# SPEC v0.8 §6.5: known playback_error codes; unknown ones are shown neutrally.
PROBLEM_TEXTS = {
    ("podcast", "feed_error"): "Der Podcast-Feed lässt sich nicht laden. Bitte die Feed-Adresse "
    "prüfen.",
    ("podcast", "no_episodes"): "Der Podcast hat keine passende Folge. Folgen, die als nicht "
    "jugendfrei markiert sind, lässt die Box aus.",
    ("local", "asset_missing"): "Eine Datei fehlt auf der Box. Sie wird beim nächsten Abgleich "
    "neu geladen.",
    ("local", "empty"): "Der Inhalt hat keine Titel.",
}
CODE_TEXTS = {
    "disabled": "{provider} ist für diese Box ausgeschaltet (Einstellungen unten).",
    "not_available": "{provider} kann diese Box noch nicht abspielen.",
    "decode_error": "Eine Datei ließ sich nicht abspielen.",
    "player_restart": "Die Wiedergabe ist abgebrochen und wurde neu gestartet.",
}


def _problems(problems: list[events.PlaybackProblem]) -> list[dict[str, Any]]:
    return [
        {"text": problem_text(p.provider, p.code), "figure": p.figure, "count": p.count,
         "last_at": p.last_at}
        for p in problems
    ]  # fmt: skip


def problem_text(provider: str, code: str) -> str:
    if text := PROBLEM_TEXTS.get((provider, code)):
        return text
    name = PROVIDER_NAMES.get(provider, provider)
    if template := CODE_TEXTS.get(code):
        return template.format(provider=name)
    return f"Problem bei der Wiedergabe ({name}: {code})."


_CLAIM_MESSAGES = {
    ErrorCode.CODE_INVALID: "Der Code ist ungültig oder abgelaufen.",
    ErrorCode.DEVICE_PAIRED_ELSEWHERE: "Diese Box ist mit einem anderen Haushalt gekoppelt. "
    "Sie muss dort zuerst entfernt werden.",
    ErrorCode.RATE_LIMITED: "Zu viele Versuche. Bitte etwas später erneut versuchen.",
}


@router.get("")
async def boxes(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ReadCtx,
    error: str | None = None,
) -> Response:
    return render(
        request,
        "boxes.html",
        {"devices": await devices.list_devices(db, ctx), "error": error},
        session=session,
        ctx=ctx,
    )


@router.post("/add")
async def add_box(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ClaimCtx,
    settings: SettingsDep,
    code: Annotated[str, Form()],
    name: Annotated[str, Form(max_length=64)],
) -> Response:
    """Claim by code (SPEC §7.1); then the wizard follows the box through its setup."""
    code = "".join(ch for ch in code if ch.isdigit())
    name = name.strip()
    form = {"code": code, "name": name}
    if len(code) != 6 or not name:
        return await render_start(
            request, db, session, ctx, settings, form=form, status_code=400,
            error="Bitte den 6-stelligen Code und einen Namen angeben.",
        )  # fmt: skip
    try:
        claimed = await claim_with_limits(request, db, ctx, code, name)
    except ApiError as exc:
        return await render_start(
            request, db, session, ctx, settings, form=form, status_code=exc.status_code,
            error=_CLAIM_MESSAGES.get(exc.code, exc.message),
        )  # fmt: skip
    return RedirectResponse(f"/t/{ctx.tenant_id}/boxes/{claimed.device_id}/setup", status_code=303)


def _reported_view(
    device: Device, config_rev: int, latest: Release | None
) -> dict[str, Any] | None:
    if not device.reported:
        return None
    try:
        data = ReportedData.model_validate(device.reported)
    except ValidationError:
        return None
    soloist_warning = False
    if data.soloist and data.soloist.installed and data.soloist.build_expires_at:
        # SPEC §6.4: warn when the Soloist build expires in less than 14 days.
        soloist_warning = (
            data.soloist.build_expires_at - dt.datetime.now(dt.UTC).date() < SOLOIST_WARN
        )
    return {
        "data": data,
        "software": software_view(data, latest),
        "health": health_hints(data),
        "config_in_sync": data.applied_config_rev >= config_rev,
        "device_in_sync": data.applied_device_rev >= device.device_rev,
        "soloist_warning": soloist_warning,
    }


async def _box_page(
    request: Request,
    db: DbSession,
    session: SessionInfo,
    ctx: TenantContext,
    device_id: uuid.UUID,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    device = await devices.get_device(db, ctx, device_id)
    cfg = await devices.get_config(db, ctx, device_id)
    tenant = await db.get(Tenant, ctx.tenant_id)
    latest = request.app.state.update_channel.latest()
    return render(
        request,
        "box.html",
        {
            "device": device,
            "cfg": cfg,
            "reported": _reported_view(device, tenant.config_rev if tenant else 0, latest),
            "problems": _problems(await events.playback_problems(db, ctx, device_id)),
            "providers": PROVIDERS,
            "locales": LOCALES,
            "timezones": TIMEZONES,
            "error": error,
            "notice": notice,
        },
        session=session,
        ctx=ctx,
        status_code=status_code,
    )


@router.get("/{device_id}")
async def box(
    request: Request, db: DbSession, session: CurrentSession, ctx: ReadCtx, device_id: uuid.UUID
) -> Response:
    return await _box_page(request, db, session, ctx, device_id)


@router.post("/{device_id}/rename")
async def rename(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: RenameCtx,
    device_id: uuid.UUID,
    name: Annotated[str, Form(max_length=64)],
) -> Response:
    try:
        await devices.rename_device(db, ctx, device_id, name)
    except NotFoundError:
        raise
    except DomainError as exc:
        await db.rollback()
        return await _box_page(
            request, db, session, ctx, device_id, error=exc.message, status_code=400
        )
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/boxes/{device_id}", status_code=303)


@router.post("/{device_id}/config")
async def update_config(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ConfigCtx,
    device_id: uuid.UUID,
    max_volume: Annotated[int, Form()],
    start_volume: Annotated[int, Form()],
    on_token_removed: Annotated[str, Form()],
    locale: Annotated[str, Form()],
    timezone: Annotated[str, Form()],
    providers: Annotated[list[str] | None, Form()] = None,
    sleep_timer_min: Annotated[str, Form()] = "",
    quiet_enabled: Annotated[bool, Form()] = False,
    quiet_start: Annotated[str, Form()] = "19:30",
    quiet_end: Annotated[str, Form()] = "06:30",
    quiet_mode: Annotated[str, Form()] = "limit",
    quiet_max_volume: Annotated[int, Form()] = 25,
    auto_update: Annotated[bool, Form()] = False,
) -> Response:
    quiet: dict[str, Any] | None = None
    if quiet_enabled:
        quiet = {"start": quiet_start, "end": quiet_end}
        if quiet_mode == "lock":
            quiet["lock"] = True
        else:
            quiet["max_volume"] = quiet_max_volume
    try:
        config = DeviceConfigMsg.model_validate(
            {
                "max_volume": max_volume,
                "start_volume": start_volume,
                "quiet_hours": quiet,
                "sleep_timer_min": int(sleep_timer_min) if sleep_timer_min.strip() else None,
                "on_token_removed": on_token_removed,
                "locale": locale,
                "timezone": timezone,
                "providers_enabled": providers or [],
                "auto_update": auto_update,
            }
        )
    except (ValidationError, ValueError):
        return await _box_page(
            request, db, session, ctx, device_id,
            error="Bitte die Einstellungen prüfen (Lautstärke 0–100, Zeiten als HH:MM).",
            status_code=400,
        )  # fmt: skip
    try:
        await devices.update_config(db, ctx, device_id, config)
    except NotFoundError:
        raise
    except DomainError as exc:
        await db.rollback()
        return await _box_page(
            request, db, session, ctx, device_id, error=exc.message, status_code=400
        )
    await db.commit()
    return await _box_page(
        request, db, session, ctx, device_id, notice="Einstellungen gespeichert."
    )


@router.post("/{device_id}/remove")
async def remove(db: DbSession, ctx: RemoveCtx, device_id: uuid.UUID) -> Response:
    await devices.remove_device(db, ctx, device_id)
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/boxes", status_code=303)
