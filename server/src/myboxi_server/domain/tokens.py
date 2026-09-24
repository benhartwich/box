"""Figures (SPEC §3.5) and unknown figures from ``token_unknown`` events (SPEC §2)."""

from __future__ import annotations

import datetime as dt
import re
import uuid
from dataclasses import dataclass

from sqlalchemy import exists, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_server.domain.authz import Perm, TenantContext
from myboxi_server.domain.errors import ConflictError, InvalidInputError, NotFoundError
from myboxi_server.models import Event, Token

UID_RE = re.compile(r"^(?:[0-9A-F]{2}){4,10}$")
MAX_LABEL = 100
UNKNOWN_WINDOW = dt.timedelta(days=30)


def normalize_uid(raw: str) -> str:
    """``04:a2:b3 c4-d5`` → ``04A2B3C4D5`` (SPEC §3.5: hex, upper case, no separators)."""
    uid = re.sub(r"[\s:\-.]", "", raw).upper()
    if not UID_RE.match(uid):
        raise InvalidInputError(
            "Die UID muss aus 4 bis 10 Bytes in Hex bestehen, z. B. 04A2B3C4D5E680."
        )
    return uid


def clean_label(raw: str) -> str:
    label = raw.strip()
    if not 1 <= len(label) <= MAX_LABEL:
        raise InvalidInputError("Bitte einen Namen mit höchstens 100 Zeichen angeben.")
    return label


async def list_tokens(db: AsyncSession, ctx: TenantContext) -> list[Token]:
    ctx.require(Perm.READ)
    return list(
        await db.scalars(
            select(Token).where(Token.tenant_id == ctx.tenant_id).order_by(func.lower(Token.label))
        )
    )


async def get_token(db: AsyncSession, ctx: TenantContext, token_id: uuid.UUID) -> Token:
    ctx.require(Perm.READ)
    token = await db.scalar(
        select(Token).where(Token.id == token_id, Token.tenant_id == ctx.tenant_id)
    )
    if token is None:
        raise NotFoundError()
    return token


async def create_token(
    db: AsyncSession, ctx: TenantContext, *, uid: str, label: str, icon: str | None = None
) -> Token:
    ctx.require(Perm.TOKEN_WRITE)
    token = Token(
        tenant_id=ctx.tenant_id,
        uid=normalize_uid(uid),
        label=clean_label(label),
        icon=(icon or "").strip()[:16] or None,
    )
    try:
        async with db.begin_nested():
            db.add(token)
    except IntegrityError:
        raise ConflictError("Diese Figur gibt es schon.") from None
    return token


async def update_token(
    db: AsyncSession, ctx: TenantContext, token_id: uuid.UUID, *, label: str, icon: str | None
) -> Token:
    ctx.require(Perm.TOKEN_WRITE)
    token = await get_token(db, ctx, token_id)
    token.label = clean_label(label)
    token.icon = (icon or "").strip()[:16] or None
    await db.flush()
    return token


async def delete_token(db: AsyncSession, ctx: TenantContext, token_id: uuid.UUID) -> None:
    ctx.require(Perm.TOKEN_WRITE)
    token = await get_token(db, ctx, token_id)
    await db.delete(token)
    await db.flush()


@dataclass(frozen=True)
class UnknownToken:
    uid: str
    last_seen: dt.datetime
    count: int


async def unknown_tokens(db: AsyncSession, ctx: TenantContext) -> list[UnknownToken]:
    """UIDs the tenant's boxes reported as unknown in the last 30 days, not yet a figure."""
    ctx.require(Perm.READ)
    uid = Event.data["uid"].astext
    rows = await db.execute(
        select(uid, func.max(Event.received_at), func.count())
        .where(
            Event.tenant_id == ctx.tenant_id,
            Event.type == "token_unknown",
            Event.received_at > func.now() - UNKNOWN_WINDOW,
            ~exists().where(Token.tenant_id == ctx.tenant_id, Token.uid == uid),
        )
        .group_by(uid)
        .order_by(func.max(Event.received_at).desc())
    )
    return [UnknownToken(u, seen, n) for u, seen, n in rows.tuples() if u and UID_RE.match(u)]
