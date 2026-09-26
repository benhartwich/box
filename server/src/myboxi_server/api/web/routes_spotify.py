"""Spotify in the web UI: connect the household's own app, search, add (SPEC v0.10 §3.6)."""

from __future__ import annotations

import uuid
from typing import Annotated, Literal
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response
from pydantic import ValidationError

from myboxi_protocol.reported import ReportedData
from myboxi_server.api.web.deps import (
    ContentWriteCtx,
    CurrentSession,
    DbSession,
    ReadCtx,
    SettingsDep,
    SpotifyCtx,
    csrf_protect,
)
from myboxi_server.api.web.render import render
from myboxi_server.api.web.routes_boxes import spotify_view
from myboxi_server.domain import contents, devices, spotify, uploads
from myboxi_server.domain.authz import TenantContext
from myboxi_server.domain.errors import DomainError
from myboxi_server.ids import uuid7
from myboxi_server.models.enums import ContentKind
from myboxi_server.settings import Settings

router = APIRouter(dependencies=[Depends(csrf_protect)])


def _web(request: Request) -> spotify.SpotifyWeb:
    web: spotify.SpotifyWeb = request.app.state.spotify
    return web


@router.get("/t/{tid}/spotify")
async def spotify_page(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ReadCtx,
    settings: SettingsDep,
    error: str | None = None,
    notice: str | None = None,
) -> Response:
    return render(
        request,
        "spotify.html",
        {
            "account": await spotify.get_account(db, ctx),
            "callback_url": spotify.callback_url(settings),
            "boxes": await _boxes(db, ctx),
            "error": error,
            "notice": notice,
        },
        session=session,
        ctx=ctx,
    )


async def _boxes(db: DbSession, ctx: TenantContext) -> list[dict[str, str]]:
    """Where each box stands with Spotify (SPEC v0.9 §6.4), so the page says what is left."""
    rows: list[dict[str, str]] = []
    for device in await devices.list_devices(db, ctx):
        cfg = await devices.get_config(db, ctx, device.id)
        level, text = "", ""
        if "spotify" not in cfg.providers_enabled:
            level, text = "warn", "Spotify ist aus: auf der Box-Seite unter „Quellen“ einschalten."
        else:
            try:
                data = ReportedData.model_validate(device.reported) if device.reported else None
            except ValidationError:
                data = None
            view = spotify_view(data, expiring=False) if data is not None else None
            if view is None:
                text = "Eingeschaltet. Die Box meldet ihren Stand, sobald sie online ist."
            else:
                level, text = view["level"], view["text"]
        rows.append({"id": str(device.id), "name": device.name, "level": level, "text": text})
    return rows


def _back(ctx: TenantContext, **query: str) -> RedirectResponse:
    suffix = f"?{urlencode(query)}" if query else ""
    return RedirectResponse(f"/t/{ctx.tenant_id}/spotify{suffix}", status_code=303)


@router.post("/t/{tid}/spotify/app")
async def save_app(
    db: DbSession, ctx: SpotifyCtx, client_id: Annotated[str, Form(max_length=100)]
) -> Response:
    try:
        await spotify.save_client_id(db, ctx, client_id)
    except DomainError as exc:
        await db.rollback()
        return _back(ctx, error=exc.message)
    await db.commit()
    return _back(ctx, notice="Client-ID gespeichert. Jetzt mit Spotify verbinden.")


@router.post("/t/{tid}/spotify/connect")
async def connect(db: DbSession, ctx: SpotifyCtx, settings: SettingsDep) -> Response:
    try:
        url = await spotify.start_login(db, ctx, settings)
    except DomainError as exc:
        await db.rollback()
        return _back(ctx, error=exc.message)
    await db.commit()
    return RedirectResponse(url, status_code=303)


@router.get("/spotify/callback")
async def callback(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    settings: SettingsDep,
    state: str = "",
    code: str = "",
    error: str = "",
) -> Response:
    """Spotify sends the parent back here (the redirect URI of the household's app)."""
    web = _web(request)
    login_error = None
    tenant_id: uuid.UUID | None = None
    if error or not code or not state:
        login_error = "Die Anmeldung bei Spotify wurde abgebrochen."
    else:
        try:
            tenant_id = await web.finish_login(
                db, settings, user_id=session.user.id, state=state, code=code
            )
        except DomainError as exc:
            login_error = exc.message
    await db.commit()
    if tenant_id is None:
        return render(
            request, "error.html", {"message": login_error, "status": 400}, session=session,
            status_code=400,
        )  # fmt: skip
    return RedirectResponse(
        f"/t/{tenant_id}/spotify?notice=Spotify+ist+verbunden.", status_code=303
    )


@router.post("/t/{tid}/spotify/disconnect")
async def disconnect(request: Request, db: DbSession, ctx: SpotifyCtx) -> Response:
    await _web(request).disconnect(db, ctx)
    await db.commit()
    return _back(ctx, notice="Die Verbindung zu Spotify ist getrennt.")


async def _results(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: TenantContext,
    settings: Settings,
    *,
    query: str | None = None,
    what: Literal["playlists", "albums"] | None = None,
) -> Response:
    web = _web(request)
    items: list[spotify.Item] = []
    error = None
    try:
        if what is not None:
            items = await web.library(db, ctx, settings, what)
        elif query:
            items = await web.search(db, ctx, settings, query)
    except DomainError as exc:
        error = exc.message
    await db.commit()  # a rotated refresh token
    return render(
        request,
        "_spotify_results.html",
        {"items": items, "error": error, "query": query, "what": what},
        session=session,
        ctx=ctx,
    )


@router.get("/t/{tid}/spotify/search")
async def search(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ContentWriteCtx,
    settings: SettingsDep,
    q: str = "",
) -> Response:
    return await _results(request, db, session, ctx, settings, query=q[:100])


@router.get("/t/{tid}/spotify/library")
async def library(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ContentWriteCtx,
    settings: SettingsDep,
    what: Literal["playlists", "albums"] = "playlists",
) -> Response:
    return await _results(request, db, session, ctx, settings, what=what)


async def add_spotify_content(
    request: Request,
    db: DbSession,
    ctx: TenantContext,
    settings: Settings,
    *,
    uri: str,
    title: str,
    image: str | None,
) -> uuid.UUID:
    """Create the content (SPEC §3.6: only the URI) and take Spotify's cover along."""
    content = await contents.create_content(
        db, ctx, kind=ContentKind.SPOTIFY, title=title, source=contents.spotify_source(uri)
    )
    await db.commit()
    data = await _web(request).cover(image) if image else None
    if data:
        staged = settings.tmp_dir / f"{uuid7()}.jpg"
        staged.parent.mkdir(parents=True, exist_ok=True)
        staged.write_bytes(data)
        try:
            await uploads.set_cover(
                db, ctx, request.app.state.asset_store, settings,
                content_id=content.id, filename="cover.jpg", staged=staged,
            )  # fmt: skip
            await db.commit()
        except DomainError:
            await db.rollback()  # the content stays, only without a cover
    return content.id


@router.post("/t/{tid}/spotify/add")
async def add(
    request: Request,
    db: DbSession,
    ctx: ContentWriteCtx,
    settings: SettingsDep,
    uri: Annotated[str, Form(max_length=100)],
    title: Annotated[str, Form(max_length=300)],
    image: Annotated[str, Form(max_length=1000)] = "",
) -> Response:
    try:
        content_id = await add_spotify_content(
            request, db, ctx, settings, uri=uri, title=title, image=image or None
        )
    except DomainError as exc:
        await db.rollback()
        query = urlencode({"kind": "spotify", "error": exc.message})
        return RedirectResponse(f"/t/{ctx.tenant_id}/contents/new?{query}", status_code=303)
    return RedirectResponse(f"/t/{ctx.tenant_id}/contents/{content_id}", status_code=303)
