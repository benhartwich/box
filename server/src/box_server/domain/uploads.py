"""Uploads and transcoding (SPEC §3.8).

An uploaded file becomes part of a collection only after the transcoding job succeeded; the
content_item insert in that job's transaction is what raises the revision (SPEC §5.1).
Assets are deduplicated per tenant by the SHA-256 of the transcoded output.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import uuid
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy import func, select, text, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from box_server.domain.authz import Perm, TenantContext
from box_server.domain.errors import InvalidInputError, NotFoundError
from box_server.ids import uuid7
from box_server.media import ffmpeg
from box_server.models import Asset, Content, ContentItem, Upload
from box_server.models.enums import ContentKind, UploadProfile, UploadStatus
from box_server.settings import Settings
from box_server.storage.base import AssetStore, relpath_for

log = logging.getLogger(__name__)

OPUS_MIME = "audio/ogg; codecs=opus"
JPEG_MIME = "image/jpeg"
AUDIO_EXTENSIONS = frozenset({".mp3", ".m4a", ".wav", ".ogg", ".oga", ".opus", ".flac"})
IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
CHUNK = 1024 * 1024


def sha256_file(path: Path) -> tuple[str, int]:
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as fh:
        while chunk := fh.read(CHUNK):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


async def sha256_file_async(path: Path) -> tuple[str, int]:
    return await asyncio.to_thread(sha256_file, path)


def _unlink(path: Path) -> None:
    path.unlink(missing_ok=True)


def _exists(path: Path) -> bool:
    return path.exists()


def title_from_filename(name: str) -> str:
    stem = Path(name).stem.replace("_", " ").strip()
    return (stem or "Titel")[:200]


def check_audio_filename(name: str) -> None:
    if Path(name).suffix.lower() not in AUDIO_EXTENSIONS:
        raise InvalidInputError("Erlaubt sind mp3, m4a, wav, ogg und flac.")


async def lock_sha(db: AsyncSession, sha256: str) -> None:
    """Serializes storing and garbage-collecting the same file (transaction-scoped)."""
    await db.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": f"asset:{sha256}"})


async def _collection(db: AsyncSession, ctx: TenantContext, content_id: uuid.UUID) -> Content:
    content = await db.scalar(
        select(Content).where(Content.id == content_id, Content.tenant_id == ctx.tenant_id)
    )
    if content is None:
        raise NotFoundError()
    if content.kind != ContentKind.COLLECTION:
        raise InvalidInputError("Dateien lassen sich nur zu Sammlungen hinzufügen.")
    return content


async def create_upload(
    db: AsyncSession,
    ctx: TenantContext,
    *,
    content_id: uuid.UUID,
    filename: str,
    staged: Path,
    profile: UploadProfile,
    ffprobe: str,
) -> Upload:
    """Register a staged file (inside the store's tmp dir) for transcoding. The caller commits
    and then defers the job."""
    ctx.require(Perm.UPLOAD)
    await _collection(db, ctx, content_id)
    check_audio_filename(filename)
    try:
        await ffmpeg.check_audio(ffprobe, staged)
    except ffmpeg.MediaError as exc:
        raise InvalidInputError(exc.message) from None
    sha, size = await sha256_file_async(staged)
    upload = Upload(
        id=uuid7(),
        tenant_id=ctx.tenant_id,
        content_id=content_id,
        created_by=ctx.user_id,
        original_filename=Path(filename).name[:255],
        source_sha256=sha,
        source_bytes=size,
        tmp_path=str(staged),
        profile=profile,
    )
    db.add(upload)
    await db.flush()
    return upload


async def list_uploads(
    db: AsyncSession, ctx: TenantContext, content_id: uuid.UUID, *, open_only: bool = False
) -> list[Upload]:
    ctx.require(Perm.READ)
    stmt = select(Upload).where(Upload.tenant_id == ctx.tenant_id, Upload.content_id == content_id)
    if open_only:
        stmt = stmt.where(
            Upload.status.in_([UploadStatus.PENDING, UploadStatus.PROCESSING, UploadStatus.FAILED])
        )
    return list(await db.scalars(stmt.order_by(Upload.created_at)))


async def dismiss_upload(db: AsyncSession, ctx: TenantContext, upload_id: uuid.UUID) -> None:
    ctx.require(Perm.UPLOAD)
    upload = await db.scalar(
        select(Upload).where(Upload.id == upload_id, Upload.tenant_id == ctx.tenant_id)
    )
    if upload is None:
        raise NotFoundError()
    if upload.status in (UploadStatus.PENDING, UploadStatus.PROCESSING):
        raise InvalidInputError("Die Datei wird noch verarbeitet.")
    await db.delete(upload)


@dataclass(frozen=True)
class ProcessResult:
    status: UploadStatus
    asset_id: uuid.UUID | None = None


async def process_upload(
    maker: async_sessionmaker[AsyncSession],
    store: AssetStore,
    settings: Settings,
    upload_id: uuid.UUID,
    *,
    final_attempt: bool = True,
) -> ProcessResult | None:
    """Job body: transcode, store, attach. Returns None if the upload no longer exists."""
    async with maker() as db:
        upload = await db.get(Upload, upload_id, with_for_update=True)
        if upload is None:
            return None
        if upload.status in (UploadStatus.DONE, UploadStatus.DUPLICATE):
            return ProcessResult(upload.status, upload.asset_id)
        upload.status = UploadStatus.PROCESSING
        upload.attempts += 1
        upload.error = None
        tenant_id, content_id = upload.tenant_id, upload.content_id
        source = Path(upload.tmp_path) if upload.tmp_path else None
        profile, source_sha = upload.profile, upload.source_sha256
        filename = upload.original_filename
        await db.commit()

    try:
        async with maker() as db:
            known = await _known_output(db, tenant_id, source_sha, profile)
        if known is not None:
            sha, size, duration_ms = known
            title = title_from_filename(filename)
            output: Path | None = None
        else:
            if source is None or not await asyncio.to_thread(_exists, source):
                raise ffmpeg.MediaError("Die hochgeladene Datei ist nicht mehr vorhanden.")
            info = await ffmpeg.check_audio(settings.ffprobe_path, source)
            output = settings.tmp_dir / f"{upload_id}.opus"
            measured = await ffmpeg.measure_loudness(settings.ffmpeg_path, source)
            await ffmpeg.transcode(settings.ffmpeg_path, source, output, profile, measured)
            sha, size = await sha256_file_async(output)
            duration_ms = (await ffmpeg.probe(settings.ffprobe_path, output)).duration_ms
            title = (info.title or title_from_filename(filename))[:200]
        result = await _attach(
            maker, store, tenant_id, content_id, upload_id, sha, size, duration_ms, title, output
        )
    except ffmpeg.MediaError as exc:
        await _mark_failed(maker, upload_id, exc.message)
        if source is not None:
            await asyncio.to_thread(_unlink, source)
        return ProcessResult(UploadStatus.FAILED)
    except Exception:
        log.exception("upload processing failed", extra={"upload_id": str(upload_id)})
        await _mark_failed(
            maker,
            upload_id,
            "Die Datei konnte nicht verarbeitet werden." if final_attempt else None,
        )
        raise
    if source is not None:
        await asyncio.to_thread(_unlink, source)
    return result


async def _known_output(
    db: AsyncSession, tenant_id: uuid.UUID, source_sha: str, profile: UploadProfile
) -> tuple[str, int, int] | None:
    """Same source bytes and profile already transcoded in this tenant: reuse the asset."""
    row = (
        await db.execute(
            select(Asset.sha256, Asset.bytes, Asset.duration_ms)
            .join(Upload, (Upload.asset_id == Asset.id) & (Upload.tenant_id == Asset.tenant_id))
            .where(
                Upload.tenant_id == tenant_id,
                Upload.source_sha256 == source_sha,
                Upload.profile == profile,
                Upload.status.in_([UploadStatus.DONE, UploadStatus.DUPLICATE]),
            )
            .limit(1)
        )
    ).one_or_none()
    if row is None or row.duration_ms is None:
        return None
    return row.sha256, row.bytes, row.duration_ms


async def _attach(
    maker: async_sessionmaker[AsyncSession],
    store: AssetStore,
    tenant_id: uuid.UUID,
    content_id: uuid.UUID,
    upload_id: uuid.UUID,
    sha: str,
    size: int,
    duration_ms: int,
    title: str,
    output: Path | None,
) -> ProcessResult:
    rel = relpath_for(sha, "opus")
    async with maker() as db:
        await lock_sha(db, sha)
        if output is not None:
            await store.put(output, rel)
        elif not await store.exists(rel):
            raise ffmpeg.MediaError("Die Datei fehlt im Speicher, bitte erneut hochladen.")
        await db.execute(
            insert(Asset)
            .values(
                id=uuid7(),
                tenant_id=tenant_id,
                sha256=sha,
                mime=OPUS_MIME,
                bytes=size,
                storage_path=rel,
                duration_ms=duration_ms,
            )
            .on_conflict_do_nothing(index_elements=[Asset.tenant_id, Asset.sha256])
        )
        asset_id = (
            await db.execute(
                select(Asset.id).where(Asset.tenant_id == tenant_id, Asset.sha256 == sha)
            )
        ).scalar_one()
        # Lock the collection so concurrent jobs append at distinct positions.
        content = await db.scalar(
            select(Content.id)
            .where(Content.id == content_id, Content.tenant_id == tenant_id)
            .with_for_update()
        )
        if content is None:  # collection deleted meanwhile; the upload row is gone too
            await db.commit()
            return ProcessResult(UploadStatus.FAILED)
        existing = await db.scalar(
            select(ContentItem.id).where(
                ContentItem.content_id == content_id, ContentItem.asset_id == asset_id
            )
        )
        item_id: uuid.UUID | None = None
        if existing is None:
            next_pos = await db.scalar(
                select(func.coalesce(func.max(ContentItem.position) + 1, 0)).where(
                    ContentItem.content_id == content_id
                )
            )
            item_id = uuid7()
            db.add(
                ContentItem(
                    id=item_id,
                    tenant_id=tenant_id,
                    content_id=content_id,
                    position=next_pos or 0,
                    asset_id=asset_id,
                    title=title,
                    duration_ms=duration_ms,
                )
            )
        status = UploadStatus.DONE if existing is None else UploadStatus.DUPLICATE
        await db.execute(
            update(Upload)
            .where(Upload.id == upload_id)
            .values(status=status, asset_id=asset_id, content_item_id=item_id, tmp_path=None)
        )
        await db.commit()
        return ProcessResult(status, asset_id)


async def _mark_failed(
    maker: async_sessionmaker[AsyncSession], upload_id: uuid.UUID, message: str | None
) -> None:
    async with maker() as db:
        values: dict[str, object] = {
            "status": UploadStatus.FAILED if message else UploadStatus.PENDING
        }
        if message:
            values["error"] = message
            values["tmp_path"] = None
        await db.execute(update(Upload).where(Upload.id == upload_id).values(**values))
        await db.commit()


async def set_cover(
    db: AsyncSession,
    ctx: TenantContext,
    store: AssetStore,
    settings: Settings,
    *,
    content_id: uuid.UUID,
    filename: str,
    staged: Path,
) -> Asset:
    """Replace the content's cover with a 512x512 JPEG (SPEC v0.3 §3.8). The caller commits."""
    ctx.require(Perm.CONTENT_WRITE)
    content = await db.scalar(
        select(Content).where(Content.id == content_id, Content.tenant_id == ctx.tenant_id)
    )
    if content is None:
        raise NotFoundError()
    if Path(filename).suffix.lower() not in IMAGE_EXTENSIONS:
        raise InvalidInputError("Erlaubt sind JPEG, PNG und WebP.")
    output = settings.tmp_dir / f"{uuid7()}.jpg"
    try:
        await ffmpeg.make_cover(settings.ffmpeg_path, staged, output)
    except ffmpeg.MediaError as exc:
        raise InvalidInputError(exc.message) from None
    finally:
        await asyncio.to_thread(_unlink, staged)
    sha, size = await sha256_file_async(output)
    rel = relpath_for(sha, "jpg")
    await lock_sha(db, sha)
    await store.put(output, rel)
    await db.execute(
        insert(Asset)
        .values(
            id=uuid7(),
            tenant_id=ctx.tenant_id,
            sha256=sha,
            mime=JPEG_MIME,
            bytes=size,
            storage_path=rel,
        )
        .on_conflict_do_nothing(index_elements=[Asset.tenant_id, Asset.sha256])
    )
    asset = (
        await db.execute(select(Asset).where(Asset.tenant_id == ctx.tenant_id, Asset.sha256 == sha))
    ).scalar_one()
    content.cover_asset_id = asset.id
    await db.flush()
    return asset
