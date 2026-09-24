"""Server-side web sessions in PostgreSQL.

The cookie carries a random token; the database stores only its SHA-256 hash.
Lifetime: 30 days absolute, 14 days idle.
"""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_server.auth.tokens import hash_token, new_token
from myboxi_server.models import User, WebSession

ABSOLUTE_LIFETIME = dt.timedelta(days=30)
IDLE_TIMEOUT = dt.timedelta(days=14)
TOUCH_INTERVAL = dt.timedelta(minutes=5)


@dataclass(frozen=True)
class SessionInfo:
    session_id: uuid.UUID
    user: User
    csrf_token: str
    active_tenant_id: uuid.UUID | None


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def create_session(db: AsyncSession, user_id: uuid.UUID) -> str:
    """Create a session and return the cookie value. The caller commits."""
    token = new_token()
    db.add(
        WebSession(
            token_hash=hash_token(token),
            user_id=user_id,
            csrf_token=secrets.token_urlsafe(32),
            expires_at=_now() + ABSOLUTE_LIFETIME,
        )
    )
    await db.flush()
    return token


async def load_session(db: AsyncSession, token: str | None) -> SessionInfo | None:
    if not token:
        return None
    now = _now()
    row = (
        await db.execute(
            select(WebSession, User)
            .join(User, User.id == WebSession.user_id)
            .where(WebSession.token_hash == hash_token(token))
        )
    ).one_or_none()
    if row is None:
        return None
    ws, user = row
    if ws.expires_at <= now or ws.last_seen_at + IDLE_TIMEOUT <= now or not user.is_active:
        await db.execute(delete(WebSession).where(WebSession.id == ws.id))
        await db.commit()
        return None
    if now - ws.last_seen_at > TOUCH_INTERVAL:
        await db.execute(update(WebSession).where(WebSession.id == ws.id).values(last_seen_at=now))
        await db.commit()
    return SessionInfo(ws.id, user, ws.csrf_token, ws.active_tenant_id)


async def delete_session(db: AsyncSession, token: str) -> None:
    await db.execute(delete(WebSession).where(WebSession.token_hash == hash_token(token)))
    await db.commit()


async def delete_user_sessions(db: AsyncSession, user_id: uuid.UUID) -> None:
    await db.execute(delete(WebSession).where(WebSession.user_id == user_id))
