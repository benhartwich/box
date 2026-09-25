"""Order requests for printed cases ("Box gestalten", docs/gehaeuse.md).

Not tenant data and not part of the device protocol: anyone can ask for a printed case.
"""

from __future__ import annotations

import datetime as dt
from typing import Any

from sqlalchemy import DateTime, Index, LargeBinary, SmallInteger, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from myboxi_server.models.base import Base, Timestamps, UuidPk, pg_enum
from myboxi_server.models.enums import CaseRequestStatus


class CaseRequest(UuidPk, Timestamps, Base):
    __tablename__ = "case_request"
    __table_args__ = (Index("ix_case_request_status", "status"),)

    email: Mapped[str] = mapped_column(Text)
    contact_name: Mapped[str] = mapped_column(Text)
    country: Mapped[str] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(SmallInteger)
    message: Mapped[str] = mapped_column(Text, server_default="")
    config: Mapped[dict[str, Any]] = mapped_column(JSONB)
    config_digest: Mapped[str] = mapped_column(Text)
    generator_version: Mapped[str] = mapped_column(Text)
    status: Mapped[CaseRequestStatus] = mapped_column(
        pg_enum(CaseRequestStatus, "case_request_status"),
        server_default=CaseRequestStatus.UNCONFIRMED.value,
    )
    # Confirmation link: only the hash is stored; cleared once confirmed.
    token_hash: Mapped[bytes | None] = mapped_column(LargeBinary, unique=True)
    token_expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    confirmed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
