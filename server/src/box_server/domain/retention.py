"""Data retention (SPEC §3.11, §10): events 30 days; expired auth artefacts."""

from __future__ import annotations

import datetime as dt

from sqlalchemy import delete, or_
from sqlalchemy.ext.asyncio import AsyncSession

from box_server.auth.sessions import IDLE_TIMEOUT
from box_server.models import Event, Invitation, Pairing, RateLimit, WebSession

EVENT_RETENTION = dt.timedelta(days=30)
PAIRING_RETENTION = dt.timedelta(days=1)
INVITATION_RETENTION = dt.timedelta(days=30)
RATE_LIMIT_RETENTION = dt.timedelta(days=1)


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
