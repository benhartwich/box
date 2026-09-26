"""Devices, device configuration and pairing (SPEC §3.3, §3.4, §7.1)."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    ARRAY,
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    LargeBinary,
    SmallInteger,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from myboxi_server.models.base import Base, Timestamps, UuidPk, pg_enum
from myboxi_server.models.enums import PROVIDERS, OnTokenRemoved


class Device(Timestamps, Base):
    __tablename__ = "device"
    __table_args__ = (UniqueConstraint("tenant_id", "id"),)

    # SPEC §3.3: generated on the box.
    id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("tenant.id", ondelete="SET NULL")
    )
    name: Mapped[str] = mapped_column(Text, server_default="")
    hw_model: Mapped[str] = mapped_column(Text)
    agent_version: Mapped[str] = mapped_column(Text)
    # Argon2id hash of the device secret (SPEC §10); NULL while unpaired.
    secret_hash: Mapped[str | None] = mapped_column(Text)
    # SPEC v0.12 §7.1: SHA-256 of the box's pairing key, bound at the first pairing start.
    pairing_key_hash: Mapped[str | None] = mapped_column(Text)
    # Bumped on every pairing and unpair; device JWTs carry it and become invalid on change.
    auth_generation: Mapped[int] = mapped_column(Integer, server_default="0")
    # SPEC §5.1: raised by database triggers only.
    device_rev: Mapped[int] = mapped_column(BigInteger, server_default="0")
    reported: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    reported_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    mqtt_provisioned: Mapped[bool] = mapped_column(server_default="false")
    # SPEC §6 (M2): set at pairing (sealed), the MQTT service creates the broker account and
    # clears it; mqtt_revoke asks the service to delete the account (unpair).
    mqtt_password: Mapped[bytes | None] = mapped_column(LargeBinary)
    mqtt_revoke: Mapped[bool] = mapped_column(server_default="false")
    # SPEC §6.6: the box's last will; None until it connected once.
    mqtt_online: Mapped[bool | None] = mapped_column()
    last_seen_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    paired_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


_PROVIDER_ARRAY = "ARRAY[" + ",".join(f"'{p}'" for p in PROVIDERS) + "]::text[]"


class DeviceConfig(Timestamps, Base):
    """Desired state per box (SPEC §3.4)."""

    __tablename__ = "device_config"
    __table_args__ = (
        ForeignKeyConstraint(
            ["tenant_id", "device_id"], ["device.tenant_id", "device.id"], ondelete="CASCADE"
        ),
        CheckConstraint("max_volume BETWEEN 0 AND 100", name="max_volume_range"),
        CheckConstraint("start_volume BETWEEN 0 AND 100", name="start_volume_range"),
        CheckConstraint("sleep_timer_min IS NULL OR sleep_timer_min > 0", name="sleep_timer_pos"),
        CheckConstraint(f"providers_enabled <@ {_PROVIDER_ARRAY}", name="providers_known"),
        CheckConstraint(
            "quiet_hours IS NULL OR jsonb_typeof(quiet_hours) = 'object'", name="quiet_hours_obj"
        ),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    max_volume: Mapped[int] = mapped_column(SmallInteger, server_default="55")
    start_volume: Mapped[int] = mapped_column(SmallInteger, server_default="35")
    quiet_hours: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    sleep_timer_min: Mapped[int | None] = mapped_column(Integer)
    on_token_removed: Mapped[OnTokenRemoved] = mapped_column(
        pg_enum(OnTokenRemoved, "on_token_removed"), server_default=OnTokenRemoved.PAUSE.value
    )
    locale: Mapped[str] = mapped_column(Text, server_default="de-AT")
    timezone: Mapped[str] = mapped_column(Text, server_default="Europe/Vienna")
    providers_enabled: Mapped[list[str]] = mapped_column(
        ARRAY(Text), server_default=text("ARRAY['local','podcast','stream']::text[]")
    )
    auto_update: Mapped[bool] = mapped_column(server_default=text("true"))  # SPEC v0.7 §3.4
    spotify_allow_explicit: Mapped[bool] = mapped_column(server_default=text("false"))  # v0.9


class Pairing(UuidPk, Timestamps, Base):
    """One pairing attempt (SPEC §7.1). The device secret is never stored here."""

    __tablename__ = "pairing"
    __table_args__ = (
        CheckConstraint(r"code ~ '^[0-9]{6}$'", name="code_format"),
        Index(
            "uq_pairing_open_code",
            "code",
            unique=True,
            postgresql_where=text("claimed_at IS NULL AND invalidated_at IS NULL"),
        ),
    )

    device_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("device.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(Text)
    # SHA-256 of the poll token.
    poll_token_hash: Mapped[bytes] = mapped_column(LargeBinary, unique=True)
    hw_model: Mapped[str] = mapped_column(Text)
    agent_version: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )
    tenant_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    device_name: Mapped[str | None] = mapped_column(Text)
    delivered_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    invalidated_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceCommand(Timestamps, Base):
    """A remote command (SPEC §6.2), written by the web UI, sent by the MQTT service.

    ``id`` is the envelope id (ULID) the box acknowledges with ``cmd/ack`` (§6.3).
    """

    __tablename__ = "device_command"
    __table_args__ = (
        Index("ix_device_command_device_created", "device_id", "created_at"),
        # The MQTT service looks for waiting commands every second.
        Index(
            "ix_device_command_waiting",
            "created_at",
            postgresql_where=text("result IS NULL"),
        ),
    )

    id: Mapped[str] = mapped_column(Text, primary_key=True)
    # Not a composite key with the device: unpairing clears device.tenant_id.
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    device_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("device.id", ondelete="CASCADE"))
    name: Mapped[str] = mapped_column(Text)
    args: Mapped[dict[str, Any]] = mapped_column(JSONB, server_default="{}")
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    # ok, expired, rejected, error (SPEC §6.3); None while waiting.
    result: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    acked_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
