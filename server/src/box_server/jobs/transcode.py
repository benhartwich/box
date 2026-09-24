"""Transcoding jobs (SPEC §3.8)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid

from procrastinate import JobContext
from procrastinate.exceptions import AlreadyEnqueued
from sqlalchemy import select, update

from box_server.domain.uploads import process_upload
from box_server.jobs.app import job_app
from box_server.jobs.context import job_sessionmaker, job_settings, job_store
from box_server.models import Upload
from box_server.models.enums import UploadStatus

log = logging.getLogger(__name__)

RETRIES = 2
STALE_PENDING = dt.timedelta(minutes=15)
STALE_PROCESSING = dt.timedelta(hours=2)


def queueing_lock(upload_id: uuid.UUID | str) -> str:
    return f"upload:{upload_id}"


@job_app.task(name="transcode_upload", queue="media", retry=RETRIES, pass_context=True)
async def transcode_upload(context: JobContext, upload_id: str) -> None:
    attempts = context.job.attempts if context.job else 0
    await process_upload(
        job_sessionmaker(),
        job_store(),
        job_settings(),
        uuid.UUID(upload_id),
        final_attempt=attempts >= RETRIES,
    )


@job_app.periodic(cron="*/10 * * * *", periodic_id="requeue_uploads")
@job_app.task(name="requeue_uploads", queue="maintenance", queueing_lock="requeue_uploads")
async def requeue_uploads(timestamp: int) -> None:
    """Re-defer uploads whose job got lost (defer happens after the upload's commit)."""
    now = dt.datetime.now(dt.UTC)
    async with job_sessionmaker()() as db:
        await db.execute(
            update(Upload)
            .where(
                Upload.status == UploadStatus.PROCESSING,
                Upload.updated_at < now - STALE_PROCESSING,
            )
            .values(status=UploadStatus.PENDING)
        )
        await db.commit()
        stale = (
            await db.scalars(
                select(Upload.id).where(
                    Upload.status == UploadStatus.PENDING, Upload.updated_at < now - STALE_PENDING
                )
            )
        ).all()
    for upload_id in stale:
        try:
            await transcode_upload.configure(queueing_lock=queueing_lock(upload_id)).defer_async(
                upload_id=str(upload_id)
            )
        except AlreadyEnqueued:
            continue
    if stale:
        log.info("requeued uploads", extra={"count": len(stale), "scheduled_at": timestamp})
