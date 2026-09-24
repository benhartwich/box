"""Events from boxes (SPEC §3.11) and rate limit counters."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from myboxi_server.models.base import Base
from myboxi_server.models.enums import EVENT_TYPES

_EVENT_TYPE_LIST = ",".join(f"'{t}'" for t in EVENT_TYPES)


class Event(Base):
    """Deduplicated per ``(device_id, id)``; kept for 30 days (SPEC §3.11)."""

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint(f"type IN ({_EVENT_TYPE_LIST})", name="type_known"),
        Index("ix_event_tenant_type_received", "tenant_id", "type", "received_at"),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), primary_key=True
    )
    id: Mapped[str] = mapped_column(Text, primary_key=True)  # ULID from the box
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    type: Mapped[str] = mapped_column(Text)
    data: Mapped[dict[str, Any]] = mapped_column(JSONB)
    device_ts: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    boot_id: Mapped[uuid.UUID | None] = mapped_column()
    mono_ms: Mapped[int | None] = mapped_column(BigInteger)
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class RateLimit(Base):
    """Fixed-window counters (see auth/ratelimit.py)."""

    __tablename__ = "rate_limit"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    window_start: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer, server_default="0")
