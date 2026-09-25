"""``reported`` state (SPEC §6.4); via MQTT or ``POST /device/reported`` (§7.3)."""

from __future__ import annotations

import datetime as dt
from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from myboxi_protocol.common import NonNegativeInt, Percent, ProtocolModel
from myboxi_protocol.envelope import EnvelopeBase

PlaybackStatus = Literal["stopped", "playing", "paused"]
HealthLevel = Literal["ok", "warn", "fail"]
# Machine codes, not a closed set: a newer box may add checks an older server does not know.
HealthCode = Annotated[str, StringConstraints(pattern=r"^[a-z0-9_]{1,32}$")]


class Battery(ProtocolModel):
    percent: Percent
    charging: bool


class Storage(ProtocolModel):
    free_mb: NonNegativeInt


class Playback(ProtocolModel):
    status: PlaybackStatus
    token_id: UUID | None = None
    volume: Percent


# SPEC v0.9 §6.4: no_key, installing, starting, ready, expired, failed. A machine code, not a
# closed set, so a newer box does not break an older server.
SOLOIST_STATES = ("no_key", "installing", "starting", "ready", "expired", "failed")


class Soloist(ProtocolModel):
    """Spotify on the box (SPEC §6.4, v0.9 §8.1)."""

    installed: bool
    build_expires_at: dt.date | None = None
    state: HealthCode | None = None
    logged_in: bool | None = None
    device_name: Annotated[str, StringConstraints(max_length=64)] | None = None


class HealthCheck(ProtocolModel):
    """One self-test result (SPEC §6.4). No free text: codes only."""

    check: HealthCode
    level: HealthLevel
    code: HealthCode


class ButtonTest(ProtocolModel):
    """Buttons pressed since the setup phase began (SPEC §6.4, §9.6)."""

    seen: Annotated[list[HealthCode], Field(max_length=16)]


UpdateState = Literal[
    "up_to_date", "available", "downloading", "waiting", "installed", "failed", "rolled_back"
]


class UpdateStatus(ProtocolModel):
    """Software updates of the box (SPEC v0.7 §6.4, §11.1)."""

    state: UpdateState
    version: Annotated[str, StringConstraints(max_length=32)] | None = None
    code: HealthCode | None = None


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
    # SPEC v0.6
    health: Annotated[list[HealthCheck], Field(max_length=32)] | None = None
    button_test: ButtonTest | None = None
    # SPEC v0.7
    update: UpdateStatus | None = None


class ReportedMessage(EnvelopeBase):
    type: Literal["reported"] = "reported"
    data: ReportedData
