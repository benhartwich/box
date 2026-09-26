"""Contents: collections with uploads, podcasts, Spotify, streams (SPEC §3.6-§3.8)."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from myboxi_server.api.web.deps import (
    ContentWriteCtx,
    CurrentSession,
    DbSession,
    ReadCtx,
    SettingsDep,
    UploadCtx,
    csrf_protect,
    is_htmx,
)
from myboxi_server.api.web.render import render
from myboxi_server.api.web.routes_spotify import add_spotify_content
from myboxi_server.auth.sessions import SessionInfo
from myboxi_server.domain import contents, spotify, uploads
from myboxi_server.domain.authz import TenantContext
from myboxi_server.domain.errors import DomainError, InvalidInputError, NotFoundError
from myboxi_server.ids import uuid7
from myboxi_server.jobs.transcode import queueing_lock
from myboxi_server.models import Asset
from myboxi_server.models.enums import ContentKind, UploadProfile, UploadStatus
from myboxi_server.settings import Settings
from myboxi_server.storage.filesystem import FilesystemAssetStore

router = APIRouter(prefix="/t/{tid}", dependencies=[Depends(csrf_protect)])

KIND_LABELS = {
    ContentKind.COLLECTION: "Eigene Dateien",
    ContentKind.PODCAST: "Podcast",
    ContentKind.SPOTIFY: "Spotify",
    ContentKind.STREAM: "Radio",
}
KIND_HEADINGS = {
    ContentKind.COLLECTION: "Dateien hochladen",
    ContentKind.PODCAST: "Podcast hinzufügen",
    ContentKind.SPOTIFY: "Spotify hinzufügen",
    ContentKind.STREAM: "Radiosender hinzufügen",
}


@dataclass(frozen=True)
class KindChoice:
    kind: ContentKind
    icon: str
    title: str
    text: str
    ready: bool  # the box can play it (SPEC §8)


KIND_CHOICES = (
    KindChoice(
        ContentKind.COLLECTION, "upload", "Dateien hochladen",
        "Hörspiele, Musik oder eigene Aufnahmen als MP3, M4A oder WAV. Spielt auch ohne Internet.",
        ready=True,
    ),
    KindChoice(
        ContentKind.PODCAST, "rss", "Podcast",
        "Neue Folgen kommen von selbst auf die Box und spielen auch ohne Internet.", ready=True,
    ),
    KindChoice(
        ContentKind.SPOTIFY, "disc", "Spotify",
        "Album oder Playlist suchen oder Link einfügen. Braucht Spotify Premium und Internet.",
        ready=True,
    ),
    KindChoice(
        ContentKind.STREAM, "radio", "Radio",
        "Internetradio über eine Stream-Adresse.", ready=False,
    ),
)  # fmt: skip
PROFILES = (
    (UploadProfile.MUSIC, "Musik (Stereo)"),
    (UploadProfile.SPEECH, "Sprache/Hörspiel (Mono)"),
)
STATUS_LABELS = {
    UploadStatus.PENDING: "wartet",
    UploadStatus.PROCESSING: "wird umgewandelt",
    UploadStatus.DONE: "fertig",
    UploadStatus.DUPLICATE: "bereits vorhanden",
    UploadStatus.FAILED: "fehlgeschlagen",
}
MAX_COVER_BYTES = 10 * 1024 * 1024


@router.get("/contents")
async def content_list(
    request: Request, db: DbSession, session: CurrentSession, ctx: ReadCtx
) -> Response:
    return render(
        request,
        "contents.html",
        {
            "contents": await contents.list_contents(db, ctx),
            "kind_labels": KIND_LABELS,
            "kind_choices": KIND_CHOICES,
        },
        session=session,
        ctx=ctx,
    )


async def _spotify_ready(db: DbSession, ctx: TenantContext) -> bool:
    """The household connected its own Spotify app (SPEC v0.10 §3.6)."""
    account = await spotify.get_account(db, ctx)
    return account is not None and account.refresh_token is not None


@router.get("/contents/new")
async def new_content_form(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ContentWriteCtx,
    kind: ContentKind = ContentKind.COLLECTION,
    error: str | None = None,
) -> Response:
    return render(
        request,
        "content_new.html",
        {
            "kind": kind, "kind_headings": KIND_HEADINGS, "profiles": PROFILES, "form": {},
            "spotify_ready": kind == ContentKind.SPOTIFY and await _spotify_ready(db, ctx),
            "error": error,
        },
        session=session,
        ctx=ctx,
    )  # fmt: skip


def _source(kind: ContentKind, form: dict[str, str]) -> dict[str, Any]:
    match kind:
        case ContentKind.COLLECTION:
            return {}
        case ContentKind.PODCAST:
            try:
                keep = int(form.get("keep_latest") or 5)
            except ValueError:
                keep = 0
            return contents.podcast_source(
                form.get("feed_url", ""), keep, form.get("order", "newest_first")
            )
        case ContentKind.SPOTIFY:
            return contents.spotify_source(form.get("uri", ""))
        case ContentKind.STREAM:
            return contents.stream_source(form.get("url", ""))


async def _form_fields(request: Request) -> dict[str, str]:
    form = await request.form()
    return {k: v for k, v in form.items() if isinstance(v, str)}


@router.post("/contents")
async def create_content(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ContentWriteCtx,
    settings: SettingsDep,
    kind: Annotated[ContentKind, Form()],
    title: Annotated[str, Form(max_length=300)] = "",
    files: Annotated[list[UploadFile] | None, File()] = None,
    profile: Annotated[UploadProfile, Form()] = UploadProfile.MUSIC,
) -> Response:
    """Create a content; a collection comes with its first files in the same form."""
    fields = await _form_fields(request)
    try:
        if kind == ContentKind.SPOTIFY:
            content_id = await _create_spotify(request, db, ctx, settings, title, fields)
            return RedirectResponse(f"/t/{ctx.tenant_id}/contents/{content_id}", status_code=303)
        content = await contents.create_content(
            db, ctx, kind=kind, title=title, source=_source(kind, fields)
        )
    except DomainError as exc:
        await db.rollback()
        return render(
            request,
            "content_new.html",
            {
                "kind": kind,
                "kind_headings": KIND_HEADINGS,
                "profiles": PROFILES,
                "form": fields,
                "error": exc.message,
                "spotify_ready": kind == ContentKind.SPOTIFY and await _spotify_ready(db, ctx),
            },
            session=session,
            ctx=ctx,
            status_code=400,
        )
    await db.commit()
    if kind == ContentKind.COLLECTION and files:
        return await _accept_files(request, db, settings, session, ctx, content.id, files, profile)
    return RedirectResponse(f"/t/{ctx.tenant_id}/contents/{content.id}", status_code=303)


async def _create_spotify(
    request: Request,
    db: DbSession,
    ctx: TenantContext,
    settings: Settings,
    title: str,
    fields: dict[str, str],
) -> uuid.UUID:
    """A pasted link; with a connected Spotify app title and cover come from Spotify."""
    link = fields.get("uri", "")
    item: spotify.Item | None = None
    if await _spotify_ready(db, ctx):
        web: spotify.SpotifyWeb = request.app.state.spotify
        try:
            item = await web.lookup(db, ctx, settings, link)
        except DomainError:
            item = None  # the link alone is enough (SPEC §3.6)
    title = title.strip() or (item.name if item else "")
    if not title:
        raise InvalidInputError("Bitte einen Titel angeben.")
    return await add_spotify_content(
        request, db, ctx, settings,
        uri=item.uri if item else link, title=title, image=item.image_large if item else None,
    )  # fmt: skip


async def _tracks_context(
    db: DbSession, ctx: TenantContext, content_id: uuid.UUID
) -> dict[str, Any]:
    open_uploads = await uploads.list_uploads(db, ctx, content_id, open_only=True)
    return {
        "items": await contents.list_items(db, ctx, content_id),
        "uploads": open_uploads,
        "status_labels": STATUS_LABELS,
        "polling": any(
            u.status in (UploadStatus.PENDING, UploadStatus.PROCESSING) for u in open_uploads
        ),
    }


async def _content_page(
    request: Request,
    db: DbSession,
    session: SessionInfo,
    ctx: TenantContext,
    content_id: uuid.UUID,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    content = await contents.get_content(db, ctx, content_id)
    data: dict[str, Any] = {
        "content": content,
        "kind_labels": KIND_LABELS,
        "profiles": PROFILES,
        "error": error,
        "notice": notice,
    }
    if content.kind == ContentKind.COLLECTION:
        data |= await _tracks_context(db, ctx, content_id)
    return render(request, "content.html", data, session=session, ctx=ctx, status_code=status_code)


@router.get("/contents/{content_id}")
async def content_page(
    request: Request, db: DbSession, session: CurrentSession, ctx: ReadCtx, content_id: uuid.UUID
) -> Response:
    return await _content_page(request, db, session, ctx, content_id)


@router.get("/contents/{content_id}/tracks")
async def tracks_partial(
    request: Request, db: DbSession, session: CurrentSession, ctx: ReadCtx, content_id: uuid.UUID
) -> Response:
    """HTMX: track list and upload status, polled while uploads are being processed."""
    content = await contents.get_content(db, ctx, content_id)
    data = {"content": content} | await _tracks_context(db, ctx, content_id)
    return render(request, "_tracks.html", data, session=session, ctx=ctx)


async def _after_change(
    request: Request, db: DbSession, session: SessionInfo, ctx: TenantContext, content_id: uuid.UUID
) -> Response:
    if is_htmx(request):
        return await tracks_partial(request, db, session, ctx, content_id)
    return RedirectResponse(f"/t/{ctx.tenant_id}/contents/{content_id}", status_code=303)


@router.post("/contents/{content_id}")
async def update_content(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ContentWriteCtx,
    content_id: uuid.UUID,
    title: Annotated[str, Form(max_length=300)],
) -> Response:
    try:
        content = await contents.get_content(db, ctx, content_id)
        source = (
            None
            if content.kind == ContentKind.COLLECTION
            else _source(content.kind, await _form_fields(request))
        )
        await contents.update_content(db, ctx, content_id, title=title, source=source)
    except NotFoundError:
        raise
    except DomainError as exc:
        await db.rollback()
        return await _content_page(
            request, db, session, ctx, content_id, error=exc.message, status_code=400
        )
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/contents/{content_id}", status_code=303)


@router.post("/contents/{content_id}/delete")
async def delete_content(db: DbSession, ctx: ContentWriteCtx, content_id: uuid.UUID) -> Response:
    await contents.delete_content(db, ctx, content_id)
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/contents", status_code=303)


@router.post("/contents/{content_id}/items/{item_id}/move")
async def move_item(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ContentWriteCtx,
    content_id: uuid.UUID,
    item_id: uuid.UUID,
    direction: Annotated[str, Form()],
) -> Response:
    await contents.move_item(db, ctx, content_id, item_id, -1 if direction == "up" else 1)
    await db.commit()
    return await _after_change(request, db, session, ctx, content_id)


@router.post("/contents/{content_id}/items/{item_id}/rename")
async def rename_item(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ContentWriteCtx,
    content_id: uuid.UUID,
    item_id: uuid.UUID,
    title: Annotated[str, Form(max_length=300)],
) -> Response:
    try:
        await contents.rename_item(db, ctx, content_id, item_id, title)
    except NotFoundError:
        raise
    except DomainError as exc:
        await db.rollback()
        return await _content_page(
            request, db, session, ctx, content_id, error=exc.message, status_code=400
        )
    await db.commit()
    return await _after_change(request, db, session, ctx, content_id)


@router.post("/contents/{content_id}/items/{item_id}/delete")
async def delete_item(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: ContentWriteCtx,
    content_id: uuid.UUID,
    item_id: uuid.UUID,
) -> Response:
    await contents.delete_item(db, ctx, content_id, item_id)
    await db.commit()
    return await _after_change(request, db, session, ctx, content_id)


def _stage_blocking(file: UploadFile, dest: Path, limit: int) -> int:
    written = 0
    with dest.open("wb") as out:
        while chunk := file.file.read(1024 * 1024):
            written += len(chunk)
            if written > limit:
                raise InvalidInputError(f"Die Datei „{file.filename}“ ist zu groß.")
            out.write(chunk)
    return written


async def _stage(settings: Settings, file: UploadFile, limit: int) -> Path:
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    suffix = Path(file.filename or "").suffix.lower()[:8]
    dest = settings.tmp_dir / f"{uuid7()}{suffix}"
    try:
        await asyncio.to_thread(_stage_blocking, file, dest, limit)
    except BaseException:
        dest.unlink(missing_ok=True)
        raise
    return dest


@router.post("/contents/{content_id}/uploads")
async def upload_files(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    session: CurrentSession,
    ctx: UploadCtx,
    content_id: uuid.UUID,
    files: Annotated[list[UploadFile], File()],
    profile: Annotated[UploadProfile, Form()] = UploadProfile.MUSIC,
) -> Response:
    await contents.get_content(db, ctx, content_id)
    return await _accept_files(request, db, settings, session, ctx, content_id, files, profile)


async def _accept_files(
    request: Request,
    db: DbSession,
    settings: Settings,
    session: SessionInfo,
    ctx: TenantContext,
    content_id: uuid.UUID,
    files: list[UploadFile],
    profile: UploadProfile,
) -> Response:
    """Stage every file, register it and defer transcoding (SPEC §3.8)."""
    limit = settings.max_upload_mb * 1024 * 1024
    errors: list[str] = []
    created: list[uuid.UUID] = []
    for file in files:
        if not file.filename:
            continue
        staged: Path | None = None
        try:
            uploads.check_audio_filename(file.filename)
            staged = await _stage(settings, file, limit)
            upload = await uploads.create_upload(
                db, ctx, content_id=content_id, filename=file.filename, staged=staged,
                profile=profile, ffprobe=settings.ffprobe_path,
            )  # fmt: skip
            await db.commit()
            created.append(upload.id)
        except NotFoundError:
            raise
        except DomainError as exc:
            await db.rollback()
            if staged is not None:
                staged.unlink(missing_ok=True)
            errors.append(f"{file.filename}: {exc.message}")
    job_app = request.app.state.job_app
    for upload_id in created:
        await job_app.configure_task(
            "transcode_upload", queueing_lock=queueing_lock(upload_id)
        ).defer_async(upload_id=str(upload_id))
    if errors:
        return await _content_page(
            request, db, session, ctx, content_id, error=" ".join(errors), status_code=400
        )
    return RedirectResponse(f"/t/{ctx.tenant_id}/contents/{content_id}", status_code=303)


@router.post("/contents/{content_id}/uploads/{upload_id}/dismiss")
async def dismiss_upload(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: UploadCtx,
    content_id: uuid.UUID,
    upload_id: uuid.UUID,
) -> Response:
    await contents.get_content(db, ctx, content_id)
    try:
        await uploads.dismiss_upload(db, ctx, upload_id)
    except NotFoundError:
        raise
    except DomainError as exc:
        return await _content_page(
            request, db, session, ctx, content_id, error=exc.message, status_code=400
        )
    await db.commit()
    return await _after_change(request, db, session, ctx, content_id)


@router.post("/contents/{content_id}/cover")
async def upload_cover(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    session: CurrentSession,
    ctx: ContentWriteCtx,
    content_id: uuid.UUID,
    file: Annotated[UploadFile, File()],
) -> Response:
    await contents.get_content(db, ctx, content_id)
    try:
        staged = await _stage(settings, file, MAX_COVER_BYTES)
        await uploads.set_cover(
            db, ctx, request.app.state.asset_store, settings,
            content_id=content_id, filename=file.filename or "", staged=staged,
        )  # fmt: skip
    except NotFoundError:
        raise
    except DomainError as exc:
        await db.rollback()
        return await _content_page(
            request, db, session, ctx, content_id, error=exc.message, status_code=400
        )
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/contents/{content_id}", status_code=303)


@router.get("/covers/{asset_id}")
async def cover_image(
    request: Request, db: DbSession, ctx: ReadCtx, asset_id: uuid.UUID
) -> Response:
    asset = await db.scalar(
        select(Asset).where(
            Asset.id == asset_id, Asset.tenant_id == ctx.tenant_id, Asset.mime == uploads.JPEG_MIME
        )
    )
    if asset is None:
        raise NotFoundError()
    store: FilesystemAssetStore = request.app.state.asset_store
    return store.response(request, asset.storage_path, etag=asset.sha256, media_type=asset.mime)
