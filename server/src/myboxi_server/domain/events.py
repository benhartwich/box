"""Event ingestion (SPEC §6.5, §7.3 ``/device/events``)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_protocol.envelope import RawEnvelope
from myboxi_protocol.events import (
    EVENT_TYPES,
    EventResult,
    ResumePositionEvent,
    event_adapter,
)
from myboxi_server.domain.authz import Perm, TenantContext
from myboxi_server.models import Event, ResumePosition, Token

PROBLEM_WINDOW = dt.timedelta(days=14)


async def ingest(
    db: AsyncSession, *, tenant_id: uuid.UUID, device_id: uuid.UUID, events: list[RawEnvelope]
) -> list[EventResult]:
    """Store valid events once per ``(device, id)``. The caller commits."""
    results: list[EventResult] = []
    for raw in events:
        if raw.type not in EVENT_TYPES:
            results.append(EventResult(id=raw.id, status="rejected", code="unknown_type"))
            continue
        try:
            event = event_adapter.validate_python(raw.model_dump())
        except ValidationError:
            results.append(EventResult(id=raw.id, status="rejected", code="invalid_request"))
            continue
        inserted = await db.scalar(
            insert(Event)
            .values(
                device_id=device_id,
                id=event.id,
                tenant_id=tenant_id,
                type=event.type,
                data=event.data.model_dump(mode="json"),
                device_ts=event.ts,
                boot_id=event.boot_id,
                mono_ms=event.mono_ms,
            )
            .on_conflict_do_nothing(index_elements=[Event.device_id, Event.id])
            .returning(Event.id)
        )
        if inserted is None:
            results.append(EventResult(id=event.id, status="duplicate"))
            continue
        if isinstance(event, ResumePositionEvent):
            await _store_resume_position(db, tenant_id, device_id, event)
        results.append(EventResult(id=event.id, status="accepted"))
    return results


async def _store_resume_position(
    db: AsyncSession, tenant_id: uuid.UUID, device_id: uuid.UUID, event: ResumePositionEvent
) -> None:
    """SPEC §5.5: last writer wins by the box's timestamp; unknown figures are ignored."""
    token_exists = await db.scalar(
        select(Token.id).where(Token.id == event.data.token_id, Token.tenant_id == tenant_id)
    )
    if token_exists is None:
        return
    stmt = insert(ResumePosition).values(
        tenant_id=tenant_id,
        token_id=event.data.token_id,
        item_index=event.data.item_index,
        position_ms=event.data.position_ms,
        item_key=event.data.item_key,
        updated_at=event.ts,
        device_id=device_id,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[ResumePosition.tenant_id, ResumePosition.token_id],
            set_={
                "item_index": stmt.excluded.item_index,
                "position_ms": stmt.excluded.position_ms,
                "item_key": stmt.excluded.item_key,
                "updated_at": stmt.excluded.updated_at,
                "device_id": stmt.excluded.device_id,
                "received_at": stmt.excluded.received_at,
            },
            where=ResumePosition.updated_at < stmt.excluded.updated_at,
        )
    )


@dataclass(frozen=True)
class PlaybackProblem:
    """``playback_error`` events of one box, grouped by figure, provider and code."""

    provider: str
    code: str
    figure: str | None
    count: int
    last_at: dt.datetime


async def playback_problems(
    db: AsyncSession,
    ctx: TenantContext,
    device_id: uuid.UUID,
    *,
    now: dt.datetime | None = None,
    limit: int = 5,
) -> list[PlaybackProblem]:
    """Recent playback errors (SPEC §6.5), newest first, e.g. a podcast feed that fails."""
    ctx.require(Perm.READ)
    since = (now or dt.datetime.now(dt.UTC)) - PROBLEM_WINDOW
    rows = (
        await db.execute(
            select(Event.data, Event.received_at)
            .where(
                Event.tenant_id == ctx.tenant_id,
                Event.device_id == device_id,
                Event.type == "playback_error",
                Event.received_at >= since,
            )
            .order_by(Event.received_at.desc())
            .limit(200)
        )
    ).all()
    groups: dict[tuple[str, str, str], list[dt.datetime]] = {}
    for data, received_at in rows:
        key = (str(data.get("token_id")), str(data.get("provider")), str(data.get("code")))
        groups.setdefault(key, []).append(received_at)
    token_ids: list[uuid.UUID] = []
    for token_id, _, _ in groups:
        try:
            token_ids.append(uuid.UUID(token_id))
        except ValueError:
            continue
    labels = {
        str(t.id): t.label
        for t in await db.scalars(
            select(Token).where(Token.tenant_id == ctx.tenant_id, Token.id.in_(token_ids))
        )
    }
    problems = [
        PlaybackProblem(provider, code, labels.get(token_id), len(times), times[0])
        for (token_id, provider, code), times in groups.items()
    ]
    return problems[:limit]
