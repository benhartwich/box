"""Pairing (SPEC §7.1)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from myboxi_protocol.common import NonNegativeInt, ProtocolModel

PairingCode = Annotated[str, StringConstraints(pattern=r"^\d{6}$")]
DeviceName = Annotated[str, StringConstraints(min_length=1, max_length=64, strip_whitespace=True)]


# SPEC v0.12 §7.1: 256 random bits the box creates once; proves on every pairing start that
# the caller is the box that first used this device id (base64url, no padding).
PairingKey = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]


class PairingStartRequest(ProtocolModel):
    device_id: UUID
    hw_model: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    agent_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    # Omitted (not null) by boxes before v0.12.
    pairing_key: PairingKey | None = Field(default=None, repr=False, exclude_if=lambda v: v is None)


class PairingStartResponse(ProtocolModel):
    code: PairingCode
    expires_in: NonNegativeInt
    poll_token: Annotated[str, StringConstraints(min_length=16, max_length=128)] = Field(repr=False)


class PairingPending(ProtocolModel):
    """202 while the code has not been claimed yet."""

    status: Literal["pending"] = "pending"
    expires_in: NonNegativeInt


class MqttCredentials(ProtocolModel):
    host: str
    port: Annotated[int, Field(ge=1, le=65535)] = 8883
    username: str
    password: str = Field(repr=False)


class PairingClaimed(ProtocolModel):
    """200 after the claim; delivered exactly once. ``mqtt`` is omitted without a broker."""

    device_secret: Annotated[str, StringConstraints(min_length=16, max_length=256)] = Field(
        repr=False
    )
    tenant_id: UUID
    # Omitted (not null) when the server runs without a broker (SPEC §7.1).
    mqtt: MqttCredentials | None = Field(default=None, exclude_if=lambda v: v is None)


class ClaimRequest(ProtocolModel):
    code: PairingCode
    name: DeviceName


class ClaimResponse(ProtocolModel):
    device_id: UUID
    name: str
