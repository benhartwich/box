"""MQTT messages server ↔ box: notify, cmd, cmd/ack (SPEC §6.1-§6.3)."""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import AwareDatetime, Field

from myboxi_protocol.common import NonNegativeInt, Percent, ProtocolModel, Ulid
from myboxi_protocol.envelope import EnvelopeBase

DEFAULT_CMD_TTL_S: Final = 60


class ConfigChangedData(ProtocolModel):
    config_rev: NonNegativeInt
    device_rev: NonNegativeInt


class NotifyMessage(EnvelopeBase):
    type: Literal["config_changed"] = "config_changed"
    data: ConfigChangedData


class NoArgs(ProtocolModel):
    pass


class SetVolumeArgs(ProtocolModel):
    volume: Percent  # clamped to max_volume by the box (SPEC §9.2)


class PlayTokenArgs(ProtocolModel):
    token_id: UUID


class _CmdBase(ProtocolModel):
    expires_at: AwareDatetime


class StopCmd(_CmdBase):
    name: Literal["stop"] = "stop"
    args: NoArgs = Field(default_factory=NoArgs)


class SetVolumeCmd(_CmdBase):
    name: Literal["set_volume"] = "set_volume"
    args: SetVolumeArgs


class PlayTokenCmd(_CmdBase):
    name: Literal["play_token"] = "play_token"
    args: PlayTokenArgs


class IdentifyCmd(_CmdBase):
    name: Literal["identify"] = "identify"
    args: NoArgs = Field(default_factory=NoArgs)


class SyncNowCmd(_CmdBase):
    name: Literal["sync_now"] = "sync_now"
    args: NoArgs = Field(default_factory=NoArgs)


class UpdateCheckCmd(_CmdBase):
    name: Literal["update_check"] = "update_check"
    args: NoArgs = Field(default_factory=NoArgs)


CmdData = Annotated[
    StopCmd | SetVolumeCmd | PlayTokenCmd | IdentifyCmd | SyncNowCmd | UpdateCheckCmd,
    Field(discriminator="name"),
]


class CmdMessage(EnvelopeBase):
    """Expired commands are discarded by the box (SPEC §6.2)."""

    type: Literal["cmd"] = "cmd"
    data: CmdData


class CmdAckData(ProtocolModel):
    cmd_id: Ulid
    result: Literal["ok", "expired", "rejected", "error"]
    message: str | None = None


class CmdAckMessage(EnvelopeBase):
    type: Literal["cmd_ack"] = "cmd_ack"
    data: CmdAckData
