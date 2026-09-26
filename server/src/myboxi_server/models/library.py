"""Figures, assets, contents, bindings, uploads, resume positions (SPEC §3.5-§3.10).

Cross references between tenant-scoped rows use composite foreign keys ``(tenant_id, x_id)``,
so the database itself refuses links across tenants (CLAUDE.md rule 7).
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from myboxi_server.models.base import Base, Timestamps, UuidPk, pg_enum
from myboxi_server.models.enums import ContentKind, RepeatMode, UploadProfile, UploadStatus


def _tenant_fk() -> Mapped[uuid.UUID]:
    return mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))


class Token(UuidPk, Timestamps, Base):
    """Figure (SPEC §3.5)."""

    __tablename__ = "token"
    __table_args__ = (
        UniqueConstraint("tenant_id", "uid"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("uid ~ '^([0-9A-F]{2}){4,10}$'", name="uid_format"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    uid: Mapped[str] = mapped_column(Text)
    label: Mapped[str] = mapped_column(Text)
    icon: Mapped[str | None] = mapped_column(Text)


class Asset(UuidPk, Timestamps, Base):
    """Content-addressed file (SPEC §3.8). The same file may back assets of several tenants."""

    __tablename__ = "asset"
    __table_args__ = (
        UniqueConstraint("tenant_id", "sha256"),
        UniqueConstraint("tenant_id", "id"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="sha256_format"),
        CheckConstraint("bytes >= 0", name="bytes_nonneg"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    sha256: Mapped[str] = mapped_column(Text, index=True)
    mime: Mapped[str] = mapped_column(Text)
    bytes: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class Content(UuidPk, Timestamps, Base):
    """SPEC §3.6. ``rev`` is raised by a database trigger once per transaction."""

    __tablename__ = "content"
    __table_args__ = (
        UniqueConstraint("tenant_id", "id"),
        ForeignKeyConstraint(
            ["tenant_id", "cover_asset_id"], ["asset.tenant_id", "asset.id"], ondelete="RESTRICT"
        ),
        CheckConstraint("jsonb_typeof(source) = 'object'", name="source_object"),
        Index("ix_content_tenant_kind", "tenant_id", "kind"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    kind: Mapped[ContentKind] = mapped_column(pg_enum(ContentKind, "content_kind"))
    title: Mapped[str] = mapped_column(Text)
    cover_asset_id: Mapped[uuid.UUID | None] = mapped_column()
    source: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    rev: Mapped[int] = mapped_column(Integer, server_default="1")


class ContentItem(UuidPk, Timestamps, Base):
    """Track of a collection (SPEC §3.7). ``(content_id, position)`` is unique, deferred."""

    __tablename__ = "content_item"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "content_id"], ["content.tenant_id", "content.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "asset_id"], ["asset.tenant_id", "asset.id"], ondelete="RESTRICT"
        ),
        UniqueConstraint("content_id", "position", deferrable=True, initially="DEFERRED"),
        CheckConstraint("position >= 0", name="position_nonneg"),
        CheckConstraint("duration_ms >= 0", name="duration_nonneg"),
        Index("ix_content_item_tenant_asset", "tenant_id", "asset_id"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    content_id: Mapped[uuid.UUID] = mapped_column()
    position: Mapped[int] = mapped_column(Integer)
    asset_id: Mapped[uuid.UUID] = mapped_column()
    title: Mapped[str] = mapped_column(Text)
    duration_ms: Mapped[int] = mapped_column(Integer)


class Binding(Timestamps, Base):
    """Figure → content, per tenant (SPEC §3.9)."""

    __tablename__ = "binding"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "token_id"], ["token.tenant_id", "token.id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["tenant_id", "content_id"], ["content.tenant_id", "content.id"], ondelete="CASCADE"
        ),
        Index("ix_binding_tenant_content", "tenant_id", "content_id"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    token_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    content_id: Mapped[uuid.UUID] = mapped_column()
    resume: Mapped[bool] = mapped_column(server_default="true")
    shuffle: Mapped[bool] = mapped_column(server_default="false")
    repeat: Mapped[RepeatMode] = mapped_column(
        pg_enum(RepeatMode, "repeat_mode"), server_default=RepeatMode.OFF.value
    )
    # SPEC v0.11 §3.9: {"id", "item_index", "position_ms", "set_at"}; the box applies it once.
    start_at: Mapped[dict[str, Any] | None] = mapped_column(JSONB)


class Upload(UuidPk, Timestamps, Base):
    """Server-side upload job state; not part of the sync model."""

    __tablename__ = "upload"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "content_id"], ["content.tenant_id", "content.id"], ondelete="CASCADE"
        ),
        Index("ix_upload_tenant_content_status", "tenant_id", "content_id", "status"),
        Index("ix_upload_tenant_source", "tenant_id", "source_sha256", "profile"),
    )

    tenant_id: Mapped[uuid.UUID] = _tenant_fk()
    content_id: Mapped[uuid.UUID] = mapped_column()
    created_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    original_filename: Mapped[str] = mapped_column(Text)
    source_sha256: Mapped[str] = mapped_column(Text)
    source_bytes: Mapped[int] = mapped_column(BigInteger)
    tmp_path: Mapped[str | None] = mapped_column(Text)
    profile: Mapped[UploadProfile] = mapped_column(pg_enum(UploadProfile, "upload_profile"))
    status: Mapped[UploadStatus] = mapped_column(
        pg_enum(UploadStatus, "upload_status"), server_default=UploadStatus.PENDING.value
    )
    error: Mapped[str | None] = mapped_column(Text)
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    # Informational; no FK so finished uploads never block asset garbage collection.
    asset_id: Mapped[uuid.UUID | None] = mapped_column()
    content_item_id: Mapped[uuid.UUID | None] = mapped_column()


class ResumePosition(Base):
    """SPEC §3.10. ``updated_at`` is the box's timestamp (last writer wins, §5.5)."""

    __tablename__ = "resume_position"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "token_id"], ["token.tenant_id", "token.id"], ondelete="CASCADE"
        ),
        CheckConstraint("item_index >= 0 AND position_ms >= 0", name="nonneg"),
    )

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), primary_key=True
    )
    token_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    item_index: Mapped[int] = mapped_column(Integer)
    position_ms: Mapped[int] = mapped_column(BigInteger)
    item_key: Mapped[str | None] = mapped_column(Text)  # SPEC v0.8 §3.10
    updated_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    device_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("device.id", ondelete="SET NULL")
    )
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    received_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
