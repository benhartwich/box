"""Event ingestion (SPEC §6.5, §7.3 ``/device/events``)."""

from __future__ import annotations

import uuid

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
from myboxi_server.models import Event, ResumePosition, Token


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
        updated_at=event.ts,
        device_id=device_id,
    )
    await db.execute(
        stmt.on_conflict_do_update(
            index_elements=[ResumePosition.tenant_id, ResumePosition.token_id],
            set_={
                "item_index": stmt.excluded.item_index,
                "position_ms": stmt.excluded.position_ms,
                "updated_at": stmt.excluded.updated_at,
                "device_id": stmt.excluded.device_id,
                "received_at": stmt.excluded.received_at,
            },
            where=ResumePosition.updated_at < stmt.excluded.updated_at,
        )
    )
