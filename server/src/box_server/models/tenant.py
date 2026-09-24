"""Tenants, users, memberships, invitations and web sessions (SPEC §3.1, §3.2)."""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    LargeBinary,
    Text,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from box_server.models.base import Base, Timestamps, UuidPk, pg_enum
from box_server.models.enums import Role, TenantPlan


class Tenant(UuidPk, Timestamps, Base):
    __tablename__ = "tenant"

    name: Mapped[str] = mapped_column(Text)
    plan: Mapped[TenantPlan] = mapped_column(
        pg_enum(TenantPlan, "tenant_plan"), server_default=TenantPlan.SELF_HOSTED.value
    )
    # SPEC §5.1: raised by database triggers only (see domain/revisions.py).
    config_rev: Mapped[int] = mapped_column(BigInteger, server_default="0")


class User(UuidPk, Timestamps, Base):
    __tablename__ = "app_user"
    __table_args__ = (Index("uq_app_user_email_lower", func.lower(text("email")), unique=True),)

    email: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str] = mapped_column(Text)
    locale: Mapped[str] = mapped_column(Text, server_default="de-AT")
    # Argon2id; NULL until an invitation has been accepted.
    password_hash: Mapped[str | None] = mapped_column(Text)
    is_active: Mapped[bool] = mapped_column(server_default="true")


class Membership(Timestamps, Base):
    __tablename__ = "membership"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), primary_key=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), primary_key=True, index=True
    )
    role: Mapped[Role] = mapped_column(pg_enum(Role, "member_role"))


class Invitation(UuidPk, Timestamps, Base):
    __tablename__ = "invitation"
    __table_args__ = (
        Index(
            "uq_invitation_open_email",
            "tenant_id",
            func.lower(text("email")),
            unique=True,
            postgresql_where=text("accepted_at IS NULL AND revoked_at IS NULL"),
        ),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    email: Mapped[str] = mapped_column(Text)
    role: Mapped[Role] = mapped_column(pg_enum(Role, "member_role"))
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    invited_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    accepted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class WebSession(UuidPk, Timestamps, Base):
    __tablename__ = "web_session"

    # SHA-256 of the cookie value; the value itself is never stored.
    token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("app_user.id", ondelete="CASCADE"), index=True
    )
    active_tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenant.id", ondelete="SET NULL")
    )
    csrf_token: Mapped[str] = mapped_column(Text)
    last_seen_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), index=True)
