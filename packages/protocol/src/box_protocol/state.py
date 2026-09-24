"""Device state: snapshot/delta and device configuration (SPEC §5.4, §3.4, §3.6)."""

from __future__ import annotations

from typing import Annotated, Literal, Self
from uuid import UUID

from pydantic import Field, StringConstraints, model_validator

from box_protocol.common import (
    ClockTime,
    HttpUrlStr,
    NfcUid,
    NonNegativeInt,
    Percent,
    ProtocolModel,
    ProviderName,
    Sha256Hex,
    ShortText,
)

RepeatMode = Literal["off", "all", "one"]
OnTokenRemoved = Literal["pause", "continue"]
SpotifyUri = Annotated[
    str,
    StringConstraints(
        pattern=r"^spotify:(?:album|playlist|track|show|episode|artist):[0-9A-Za-z]{22}$"
    ),
]


class QuietHours(ProtocolModel):
    """SPEC §3.4: exactly one of ``max_volume`` or ``lock`` (no playback at all)."""

    start: ClockTime
    end: ClockTime
    max_volume: Percent | None = None
    lock: Literal[True] | None = None

    @model_validator(mode="after")
    def _one_of(self) -> Self:
        if (self.max_volume is None) == (self.lock is None):
            raise ValueError("quiet_hours needs exactly one of max_volume or lock")
        return self


class DeviceConfig(ProtocolModel):
    """SPEC §3.4. Always transmitted completely (§5.4); defaults are the spec defaults."""

    max_volume: Percent = 55
    start_volume: Percent = 35
    quiet_hours: QuietHours | None = None
    sleep_timer_min: Annotated[int, Field(ge=1, le=24 * 60)] | None = None
    on_token_removed: OnTokenRemoved = "pause"  # noqa: S105
    locale: Annotated[str, StringConstraints(pattern=r"^[a-z]{2,3}(?:-[A-Z]{2})?$")] = "de-AT"
    timezone: Annotated[str, StringConstraints(min_length=1, max_length=64)] = "Europe/Vienna"
    providers_enabled: list[ProviderName] = Field(
        default_factory=lambda: ["local", "podcast"]  # pyright: ignore[reportUnknownLambdaType]
    )


class TokenUpsert(ProtocolModel):
    id: UUID
    uid: NfcUid
    label: ShortText


# --- content (SPEC §3.6) -------------------------------------------------------------------


class CollectionSource(ProtocolModel):
    pass


class PodcastSource(ProtocolModel):
    feed_url: HttpUrlStr
    keep_latest: Annotated[int, Field(ge=1, le=100)] = 5
    order: Literal["newest_first", "oldest_first"] = "newest_first"


class SpotifySource(ProtocolModel):
    uri: SpotifyUri


class StreamSource(ProtocolModel):
    url: HttpUrlStr


class _ContentBase(ProtocolModel):
    id: UUID
    title: ShortText
    rev: Annotated[int, Field(ge=1)]


class CollectionContent(_ContentBase):
    kind: Literal["collection"] = "collection"
    source: CollectionSource = Field(default_factory=CollectionSource)


class PodcastContent(_ContentBase):
    kind: Literal["podcast"] = "podcast"
    source: PodcastSource


class SpotifyContent(_ContentBase):
    kind: Literal["spotify"] = "spotify"
    source: SpotifySource


class StreamContent(_ContentBase):
    kind: Literal["stream"] = "stream"
    source: StreamSource


ContentUpsert = Annotated[
    CollectionContent | PodcastContent | SpotifyContent | StreamContent,
    Field(discriminator="kind"),
]
ContentKind = Literal["collection", "podcast", "spotify", "stream"]


class ContentItemUpsert(ProtocolModel):
    content_id: UUID
    position: NonNegativeInt
    asset_sha256: Sha256Hex
    bytes: NonNegativeInt
    title: ShortText
    duration_ms: NonNegativeInt


class BindingUpsert(ProtocolModel):
    token_id: UUID
    content_id: UUID
    resume: bool = True
    shuffle: bool = False
    repeat: RepeatMode = "off"


class Upserts(ProtocolModel):
    token: list[TokenUpsert] = Field(default_factory=list[TokenUpsert])
    content: list[ContentUpsert] = Field(default_factory=list[ContentUpsert])
    content_item: list[ContentItemUpsert] = Field(default_factory=list[ContentItemUpsert])
    binding: list[BindingUpsert] = Field(default_factory=list[BindingUpsert])


class Deletes(ProtocolModel):
    token: list[UUID] = Field(default_factory=list[UUID])
    content: list[UUID] = Field(default_factory=list[UUID])
    binding: list[UUID] = Field(default_factory=list[UUID], description="token ids")


class StateResponse(ProtocolModel):
    """``GET /device/state`` (SPEC §5.4). With ``full`` the box replaces its tenant slice."""

    v: Literal[1] = 1
    full: bool
    config_rev: NonNegativeInt
    device_rev: NonNegativeInt
    upserts: Upserts
    deletes: Deletes | None = None
    device_config: DeviceConfig

    @model_validator(mode="after")
    def _full_has_no_deletes(self) -> Self:
        if self.full and self.deletes is not None:
            raise ValueError("a full snapshot has no deletes")
        return self
