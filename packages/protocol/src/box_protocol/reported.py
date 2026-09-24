"""``reported`` state (SPEC §6.4); via MQTT or ``POST /device/reported`` (§7.3)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from box_protocol.common import NonNegativeInt, Percent, ProtocolModel
from box_protocol.envelope import EnvelopeBase

PlaybackStatus = Literal["stopped", "playing", "paused"]


class Battery(ProtocolModel):
    percent: Percent
    charging: bool


class Storage(ProtocolModel):
    free_mb: NonNegativeInt


class Playback(ProtocolModel):
    status: PlaybackStatus
    token_id: UUID | None = None
    volume: Percent


class Soloist(ProtocolModel):
    installed: bool
    build_expires_at: dt.date | None = None


class ReportedData(ProtocolModel):
    agent_version: Annotated[str, StringConstraints(min_length=1, max_length=32)]
    image_version: Annotated[str, StringConstraints(max_length=32)] | None = None
    hw_model: Annotated[str, StringConstraints(min_length=1, max_length=64)]
    applied_config_rev: NonNegativeInt
    applied_device_rev: NonNegativeInt
    # Optional: not every power source reports a battery level, not every box uses Wi-Fi.
    battery: Battery | None = None
    storage: Storage
    wifi_rssi: Annotated[int, Field(ge=-120, le=0)] | None = None
    time_trusted: bool
    playback: Playback
    soloist: Soloist | None = None


class ReportedMessage(EnvelopeBase):
    type: Literal["reported"] = "reported"
    data: ReportedData
