"""Contents: collections with uploads, podcasts, Spotify, streams (SPEC §3.6-§3.8)."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Annotated, Any

from fastapi import APIRouter, Depends, File, Form, Request, UploadFile
from fastapi.responses import RedirectResponse, Response
from sqlalchemy import select

from box_server.api.web.deps import (
    ContentWriteCtx,
    CurrentSession,
    DbSession,
    ReadCtx,
    SettingsDep,
    UploadCtx,
    csrf_protect,
    is_htmx,
)
from box_server.api.web.render import render
from box_server.auth.sessions import SessionInfo
from box_server.domain import contents, uploads
from box_server.domain.authz import TenantContext
from box_server.domain.errors import DomainError, InvalidInputError, NotFoundError
from box_server.ids import uuid7
from box_server.jobs.transcode import queueing_lock
from box_server.models import Asset
from box_server.models.enums import ContentKind, UploadProfile, UploadStatus
from box_server.settings import Settings
from box_server.storage.filesystem import FilesystemAssetStore

router = APIRouter(prefix="/t/{tid}", dependencies=[Depends(csrf_protect)])

KIND_LABELS = {
    ContentKind.COLLECTION: "Sammlung",
    ContentKind.PODCAST: "Podcast",
    ContentKind.SPOTIFY: "Spotify",
    ContentKind.STREAM: "Stream",
}
STATUS_LABELS = {
    UploadStatus.PENDING: "wartet",
    UploadStatus.PROCESSING: "wird umgewandelt",
    UploadStatus.DONE: "fertig",
    UploadStatus.DUPLICATE: "bereits in der Sammlung",
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
        {"contents": await contents.list_contents(db, ctx), "kind_labels": KIND_LABELS},
        session=session,
        ctx=ctx,
    )


@router.get("/contents/new")
async def new_content_form(
    request: Request,
    session: CurrentSession,
    ctx: ContentWriteCtx,
    kind: ContentKind = ContentKind.COLLECTION,
) -> Response:
    return render(
        request,
        "content_new.html",
        {"kind": kind, "kind_labels": KIND_LABELS, "form": {}},
        session=session,
        ctx=ctx,
    )


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
    kind: Annotated[ContentKind, Form()],
    title: Annotated[str, Form(max_length=300)],
) -> Response:
    fields = await _form_fields(request)
    try:
        content = await contents.create_content(
            db, ctx, kind=kind, title=title, source=_source(kind, fields)
        )
    except DomainError as exc:
        await db.rollback()
        return render(
            request,
            "content_new.html",
            {"kind": kind, "kind_labels": KIND_LABELS, "form": fields, "error": exc.message},
            session=session,
            ctx=ctx,
            status_code=400,
        )
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/contents/{content.id}", status_code=303)


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
        "profiles": [
            (UploadProfile.MUSIC, "Musik (Stereo)"),
            (UploadProfile.SPEECH, "Sprache/Hörspiel (Mono)"),
        ],
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
    """Stage every file, register it and defer transcoding (SPEC §3.8)."""
    await contents.get_content(db, ctx, content_id)
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
