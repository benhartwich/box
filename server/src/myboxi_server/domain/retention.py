"""Data retention (SPEC §3.11, §10): events 30 days; expired auth artefacts."""

from __future__ import annotations

import asyncio
import datetime as dt
from pathlib import Path

from sqlalchemy import delete, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_server.auth.sessions import IDLE_TIMEOUT
from myboxi_server.models import Event, Invitation, Pairing, RateLimit, Upload, WebSession
from myboxi_server.models.enums import UploadStatus

EVENT_RETENTION = dt.timedelta(days=30)
PAIRING_RETENTION = dt.timedelta(days=1)
INVITATION_RETENTION = dt.timedelta(days=30)
RATE_LIMIT_RETENTION = dt.timedelta(days=1)
UPLOAD_RETENTION = dt.timedelta(days=30)
TMP_FILE_RETENTION = dt.timedelta(hours=24)


async def purge_expired(db: AsyncSession, now: dt.datetime | None = None) -> dict[str, int]:
    """Delete expired rows; returns deleted counts per table. The caller commits."""
    now = now or dt.datetime.now(dt.UTC)
    statements = {
        "event": delete(Event).where(Event.received_at < now - EVENT_RETENTION),
        "web_session": delete(WebSession).where(
            or_(WebSession.expires_at < now, WebSession.last_seen_at < now - IDLE_TIMEOUT)
        ),
        "pairing": delete(Pairing).where(Pairing.expires_at < now - PAIRING_RETENTION),
        "invitation": delete(Invitation).where(Invitation.expires_at < now - INVITATION_RETENTION),
        "rate_limit": delete(RateLimit).where(RateLimit.window_start < now - RATE_LIMIT_RETENTION),
    }
    counts: dict[str, int] = {}
    for name, stmt in statements.items():
        result = await db.execute(stmt.returning(1))
        counts[name] = len(result.all())
    return counts


async def purge_tmp_files(db: AsyncSession, tmp_dir: Path, now: dt.datetime | None = None) -> int:
    """Delete staged files older than 24 h that no open upload refers to."""
    now = now or dt.datetime.now(dt.UTC)
    in_use = set(
        (
            await db.scalars(
                select(Upload.tmp_path).where(
                    Upload.tmp_path.is_not(None),
                    Upload.status.in_([UploadStatus.PENDING, UploadStatus.PROCESSING]),
                )
            )
        ).all()
    )
    cutoff = (now - TMP_FILE_RETENTION).timestamp()
    return await asyncio.to_thread(_purge_dir, tmp_dir, in_use, cutoff)


def _purge_dir(tmp_dir: Path, in_use: set[str | None], cutoff: float) -> int:
    if not tmp_dir.is_dir():
        return 0
    removed = 0
    for path in tmp_dir.iterdir():
        if path.is_file() and str(path) not in in_use and path.stat().st_mtime < cutoff:
            path.unlink(missing_ok=True)
            removed += 1
    return removed
