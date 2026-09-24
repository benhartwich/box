"""Uploads, transcoding and asset storage (SPEC §3.8, §5.1)."""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import uuid
from pathlib import Path
from typing import Any, cast

import pytest
from fastapi import FastAPI
from sqlalchemy import delete, func, select, update

from myboxi_server.domain import assets, uploads
from myboxi_server.domain.errors import InvalidInputError
from myboxi_server.domain.revisions import tenant_config_rev
from myboxi_server.jobs import context as job_context
from myboxi_server.models import Asset, Content, ContentItem, Upload
from myboxi_server.models.enums import ContentKind, UploadProfile, UploadStatus
from myboxi_server.settings import Settings
from myboxi_server.storage.filesystem import FilesystemAssetStore

from .helpers import make_tenant, owner_ctx, sessionmaker_of
from .media import color_png, loudness, probe, silence_wav, sine_mp3


@pytest.fixture(scope="session")
def media_dir(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("media")
    sine_mp3(d / "Bibi und Tina.mp3", 3.0)
    sine_mp3(d / "other.mp3", 2.0, freq=660)
    silence_wav(d / "stille.wav")
    color_png(d / "cover.png")
    (d / "notes.mp3").write_text("not audio")
    return d


async def _collection(
    app: FastAPI, tenant_id: uuid.UUID, kind: ContentKind = ContentKind.COLLECTION
) -> uuid.UUID:
    async with sessionmaker_of(app)() as db:
        source: dict[str, Any] = (
            {}
            if kind == ContentKind.COLLECTION
            else {"uri": "spotify:album:4aawyAB9vmqN3uQ7FjRGTy"}
        )
        c = Content(tenant_id=tenant_id, kind=kind, title="Sammlung", source=source)
        db.add(c)
        await db.commit()
        return c.id


def _stage(settings: Settings, src: Path) -> Path:
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)
    dest = settings.tmp_dir / f"{uuid.uuid4()}{src.suffix}"
    shutil.copyfile(src, dest)
    return dest


async def _upload(
    app: FastAPI,
    tenant_id: uuid.UUID,
    content_id: uuid.UUID,
    src: Path,
    profile: UploadProfile = UploadProfile.MUSIC,
) -> uploads.ProcessResult:
    settings: Settings = app.state.settings
    ctx = await owner_ctx(app, tenant_id)
    async with sessionmaker_of(app)() as db:
        upload = await uploads.create_upload(
            db,
            ctx,
            content_id=content_id,
            filename=src.name,
            staged=_stage(settings, src),
            profile=profile,
            ffprobe=settings.ffprobe_path,
        )
        await db.commit()
        upload_id = upload.id
    result = await uploads.process_upload(
        sessionmaker_of(app), app.state.asset_store, settings, upload_id
    )
    assert result is not None
    return result


async def test_mp3_becomes_normalized_opus_with_duration(app: FastAPI, media_dir: Path) -> None:
    t = await make_tenant(app)
    cid = await _collection(app, t.tenant_id)
    settings: Settings = app.state.settings
    ctx = await owner_ctx(app, t.tenant_id)
    async with sessionmaker_of(app)() as db:
        before = await tenant_config_rev(db, t.tenant_id)
        upload = await uploads.create_upload(
            db,
            ctx,
            content_id=cid,
            filename="Bibi und Tina.mp3",
            staged=_stage(settings, media_dir / "Bibi und Tina.mp3"),
            profile=UploadProfile.MUSIC,
            ffprobe=settings.ffprobe_path,
        )
        await db.commit()
        # Not part of the content (and no revision) before the job succeeded.
        assert await tenant_config_rev(db, t.tenant_id) == before
        assert await db.scalar(select(func.count()).select_from(ContentItem)) == 0

    result = await uploads.process_upload(
        sessionmaker_of(app), app.state.asset_store, settings, upload.id
    )
    assert result is not None
    assert result.status == UploadStatus.DONE

    async with sessionmaker_of(app)() as db:
        assert await tenant_config_rev(db, t.tenant_id) == before + 1
        asset = (await db.scalars(select(Asset))).one()
        item = (await db.scalars(select(ContentItem))).one()
        up = await db.get(Upload, upload.id)
        assert up is not None
        assert up.tmp_path is None
    assert asset.mime == "audio/ogg; codecs=opus"
    assert asset.duration_ms is not None
    assert abs(asset.duration_ms - 3000) <= 50
    assert asset.storage_path == f"{asset.sha256[:2]}/{asset.sha256[2:4]}/{asset.sha256}.opus"
    assert (item.position, item.title, item.duration_ms) == (0, "Bibi und Tina", asset.duration_ms)

    store: FilesystemAssetStore = app.state.asset_store
    path = store.path(asset.storage_path)
    data = path.read_bytes()
    assert hashlib.sha256(data).hexdigest() == asset.sha256  # SHA-256 of the output file
    assert len(data) == asset.bytes
    info = probe(path)
    stream = cast(list[dict[str, Any]], info["streams"])[0]
    assert stream["codec_name"] == "opus"
    assert stream["channels"] == 2
    assert stream["sample_rate"] == "48000"
    assert abs(loudness(path) - (-16.0)) <= 1.0  # EBU R128, -16 LUFS


async def test_speech_profile_is_mono(app: FastAPI, media_dir: Path) -> None:
    t = await make_tenant(app)
    cid = await _collection(app, t.tenant_id)
    await _upload(app, t.tenant_id, cid, media_dir / "other.mp3", UploadProfile.SPEECH)
    async with sessionmaker_of(app)() as db:
        asset = (await db.scalars(select(Asset))).one()
    store: FilesystemAssetStore = app.state.asset_store
    stream = cast(list[dict[str, Any]], probe(store.path(asset.storage_path))["streams"])[0]
    assert stream["channels"] == 1


async def test_same_file_twice_is_one_asset(app: FastAPI, media_dir: Path) -> None:
    t = await make_tenant(app)
    cid = await _collection(app, t.tenant_id)
    other = await _collection(app, t.tenant_id)
    first = await _upload(app, t.tenant_id, cid, media_dir / "Bibi und Tina.mp3")
    second = await _upload(app, t.tenant_id, cid, media_dir / "Bibi und Tina.mp3")
    third = await _upload(app, t.tenant_id, other, media_dir / "Bibi und Tina.mp3")
    assert first.status == UploadStatus.DONE
    assert second.status == UploadStatus.DUPLICATE
    assert third.status == UploadStatus.DONE
    assert first.asset_id == second.asset_id == third.asset_id
    async with sessionmaker_of(app)() as db:
        assert await db.scalar(select(func.count()).select_from(Asset)) == 1
        assert await db.scalar(select(func.count()).select_from(ContentItem)) == 2


async def test_output_is_deterministic(app: FastAPI, media_dir: Path, tmp_path: Path) -> None:
    """Dedup relies on bit-exact output: same input and profile → same SHA-256."""
    from myboxi_server.media import ffmpeg

    src = media_dir / "other.mp3"
    shas: list[str] = []
    for n in range(2):
        out = tmp_path / f"{n}.opus"
        measured = await ffmpeg.measure_loudness("ffmpeg", src)
        await ffmpeg.transcode("ffmpeg", src, out, UploadProfile.MUSIC, measured)
        shas.append(hashlib.sha256(out.read_bytes()).hexdigest())
    assert shas[0] == shas[1]


async def test_items_are_appended_in_order(app: FastAPI, media_dir: Path) -> None:
    t = await make_tenant(app)
    cid = await _collection(app, t.tenant_id)
    await _upload(app, t.tenant_id, cid, media_dir / "Bibi und Tina.mp3")
    await _upload(app, t.tenant_id, cid, media_dir / "stille.wav")
    await _upload(app, t.tenant_id, cid, media_dir / "other.mp3")
    async with sessionmaker_of(app)() as db:
        titles = (await db.scalars(select(ContentItem.title).order_by(ContentItem.position))).all()
    assert titles == ["Bibi und Tina", "stille", "other"]


async def test_rejects_non_audio_and_wrong_types(app: FastAPI, media_dir: Path) -> None:
    t = await make_tenant(app)
    cid = await _collection(app, t.tenant_id)
    spotify = await _collection(app, t.tenant_id, ContentKind.SPOTIFY)
    settings: Settings = app.state.settings
    ctx = await owner_ctx(app, t.tenant_id)
    cases = [
        (cid, "notes.mp3", media_dir / "notes.mp3"),  # not audio
        (cid, "cover.png", media_dir / "cover.png"),  # wrong extension
        (spotify, "other.mp3", media_dir / "other.mp3"),  # not a collection
    ]
    for content_id, name, src in cases:
        async with sessionmaker_of(app)() as db:
            with pytest.raises(InvalidInputError):
                await uploads.create_upload(
                    db,
                    ctx,
                    content_id=content_id,
                    filename=name,
                    staged=_stage(settings, src),
                    profile=UploadProfile.MUSIC,
                    ffprobe=settings.ffprobe_path,
                )


async def test_missing_source_fails_with_message(app: FastAPI, media_dir: Path) -> None:
    t = await make_tenant(app)
    cid = await _collection(app, t.tenant_id)
    settings: Settings = app.state.settings
    ctx = await owner_ctx(app, t.tenant_id)
    async with sessionmaker_of(app)() as db:
        staged = _stage(settings, media_dir / "other.mp3")
        upload = await uploads.create_upload(
            db,
            ctx,
            content_id=cid,
            filename="other.mp3",
            staged=staged,
            profile=UploadProfile.MUSIC,
            ffprobe=settings.ffprobe_path,
        )
        await db.commit()
    staged.unlink()
    result = await uploads.process_upload(
        sessionmaker_of(app), app.state.asset_store, settings, upload.id
    )
    assert result is not None
    assert result.status == UploadStatus.FAILED
    async with sessionmaker_of(app)() as db:
        up = await db.get(Upload, upload.id)
        assert up is not None
        assert up.error


async def test_worker_runs_deferred_job(app: FastAPI, settings: Settings, media_dir: Path) -> None:
    """The procrastinate task processes a deferred upload (as myboxi-server worker would)."""
    from myboxi_server.jobs.transcode import queueing_lock

    job_context.configure(settings)
    try:
        t = await make_tenant(app)
        cid = await _collection(app, t.tenant_id)
        ctx = await owner_ctx(app, t.tenant_id)
        async with sessionmaker_of(app)() as db:
            upload = await uploads.create_upload(
                db,
                ctx,
                content_id=cid,
                filename="other.mp3",
                staged=_stage(settings, media_dir / "other.mp3"),
                profile=UploadProfile.MUSIC,
                ffprobe=settings.ffprobe_path,
            )
            await db.commit()
        job_app = app.state.job_app
        await job_app.configure_task(
            "transcode_upload", queueing_lock=queueing_lock(upload.id)
        ).defer_async(upload_id=str(upload.id))
        await job_app.run_worker_async(queues=["media"], wait=False, install_signal_handlers=False)
        async with sessionmaker_of(app)() as db:
            up = await db.get(Upload, upload.id)
            assert up is not None
            assert up.status == UploadStatus.DONE
    finally:
        await job_context.dispose()


async def test_cover_is_square_jpeg(app: FastAPI, media_dir: Path) -> None:
    t = await make_tenant(app)
    cid = await _collection(app, t.tenant_id)
    settings: Settings = app.state.settings
    ctx = await owner_ctx(app, t.tenant_id)
    async with sessionmaker_of(app)() as db:
        asset = await uploads.set_cover(
            db,
            ctx,
            app.state.asset_store,
            settings,
            content_id=cid,
            filename="cover.png",
            staged=_stage(settings, media_dir / "cover.png"),
        )
        await db.commit()
        content = await db.get(Content, cid)
        assert content is not None
        assert content.cover_asset_id == asset.id
    assert asset.mime == "image/jpeg"
    assert asset.storage_path.endswith(".jpg")
    store: FilesystemAssetStore = app.state.asset_store
    stream = cast(list[dict[str, Any]], probe(store.path(asset.storage_path))["streams"])[0]
    assert (stream["width"], stream["height"]) == (512, 512)


async def test_garbage_collection(app: FastAPI, media_dir: Path) -> None:
    a = await make_tenant(app, "A")
    b = await make_tenant(app, "B")
    ca = await _collection(app, a.tenant_id)
    cb = await _collection(app, b.tenant_id)
    await _upload(app, a.tenant_id, ca, media_dir / "other.mp3")
    await _upload(app, b.tenant_id, cb, media_dir / "other.mp3")
    store: FilesystemAssetStore = app.state.asset_store
    async with sessionmaker_of(app)() as db:
        rows = (await db.scalars(select(Asset))).all()
        assert len(rows) == 2
        assert rows[0].storage_path == rows[1].storage_path  # one file, two tenants
        path = store.path(rows[0].storage_path)
        # A deletes its collection; the asset row becomes unreferenced and old.
        await db.execute(delete(Content).where(Content.id == ca))
        await db.execute(
            update(Asset).values(created_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=2))
        )
        await db.commit()
    assert await assets.collect_garbage(sessionmaker_of(app), store) == 1
    assert path.exists()  # still used by tenant B
    async with sessionmaker_of(app)() as db:
        await db.execute(delete(Content).where(Content.id == cb))
        await db.commit()
    assert await assets.collect_garbage(sessionmaker_of(app), store) == 1
    assert not path.exists()
