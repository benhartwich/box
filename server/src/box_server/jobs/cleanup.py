"""Periodic clean-up jobs."""

from __future__ import annotations

import logging

from procrastinate import JobContext, builtin_tasks

from box_server.domain.assets import collect_garbage
from box_server.domain.retention import purge_expired, purge_tmp_files
from box_server.jobs.app import job_app
from box_server.jobs.context import job_sessionmaker, job_settings, job_store

log = logging.getLogger(__name__)


@job_app.periodic(cron="17 3 * * *", periodic_id="purge_expired")
@job_app.task(name="purge_expired", queue="maintenance", queueing_lock="purge_expired")
async def purge_expired_task(timestamp: int) -> None:
    """Daily: events older than 30 days (SPEC §3.11) and expired sessions, codes, limits."""
    async with job_sessionmaker()() as db:
        counts = await purge_expired(db)
        await db.commit()
        counts["tmp_files"] = await purge_tmp_files(db, job_settings().tmp_dir)
    counts["assets"] = await collect_garbage(job_sessionmaker(), job_store())
    log.info("purged expired rows", extra={"counts": counts, "scheduled_at": timestamp})


@job_app.periodic(cron="37 3 * * *", periodic_id="remove_old_jobs")
@job_app.task(
    name="remove_old_jobs", queue="maintenance", queueing_lock="remove_old_jobs", pass_context=True
)
async def remove_old_jobs_task(context: JobContext, timestamp: int) -> None:
    """Keep the job queue small: finished jobs older than 7 days."""
    await builtin_tasks.remove_old_jobs(
        context, max_hours=24 * 7, remove_failed=True, remove_cancelled=True, remove_aborted=True
    )
