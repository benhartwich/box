"""Box (device) events (SPEC §6.5) and the HTTP batch format (§7.3)."""

from __future__ import annotations

from typing import Annotated, Final, Literal
from uuid import UUID

from pydantic import Field, StringConstraints, TypeAdapter

from myboxi_protocol.common import NfcUid, NonNegativeInt, ProtocolModel, ProviderName
from myboxi_protocol.envelope import EnvelopeCore, RawEnvelope

Code = Annotated[str, StringConstraints(min_length=1, max_length=64)]

MAX_EVENTS_PER_BATCH: Final = 100


class TokenUnknownData(ProtocolModel):
    uid: NfcUid


class TokenPlayedData(ProtocolModel):
    token_id: UUID
    content_id: UUID


class PlaybackErrorData(ProtocolModel):
    token_id: UUID
    provider: ProviderName
    code: Code


class StorageFullData(ProtocolModel):
    needed_mb: NonNegativeInt
    free_mb: NonNegativeInt


class SyncErrorData(ProtocolModel):
    stage: Code
    code: Code


class ResumePositionData(ProtocolModel):
    token_id: UUID
    item_index: NonNegativeInt
    position_ms: NonNegativeInt


class _EventBase(EnvelopeCore):
    # SPEC §5.6: events always carry boot id and monotonic time.
    boot_id: UUID
    mono_ms: NonNegativeInt


class TokenUnknownEvent(_EventBase):
    type: Literal["token_unknown"] = "token_unknown"
    data: TokenUnknownData


class TokenPlayedEvent(_EventBase):
    type: Literal["token_played"] = "token_played"
    data: TokenPlayedData


class PlaybackErrorEvent(_EventBase):
    type: Literal["playback_error"] = "playback_error"
    data: PlaybackErrorData


class StorageFullEvent(_EventBase):
    type: Literal["storage_full"] = "storage_full"
    data: StorageFullData


class SyncErrorEvent(_EventBase):
    type: Literal["sync_error"] = "sync_error"
    data: SyncErrorData


class ResumePositionEvent(_EventBase):
    type: Literal["resume_position"] = "resume_position"
    data: ResumePositionData


Event = Annotated[
    TokenUnknownEvent
    | TokenPlayedEvent
    | PlaybackErrorEvent
    | StorageFullEvent
    | SyncErrorEvent
    | ResumePositionEvent,
    Field(discriminator="type"),
]
EventType = Literal[
    "token_unknown",
    "token_played",
    "playback_error",
    "storage_full",
    "sync_error",
    "resume_position",
]
EVENT_TYPES: Final[frozenset[str]] = frozenset(EventType.__args__)
event_adapter: TypeAdapter[Event] = TypeAdapter(Event)


class EventBatchRequest(ProtocolModel):
    """``POST /device/events``. Envelopes are validated one by one (see ``event_adapter``)."""

    events: Annotated[list[RawEnvelope], Field(min_length=1, max_length=MAX_EVENTS_PER_BATCH)]


EventStatus = Literal["accepted", "duplicate", "rejected"]


class EventResult(ProtocolModel):
    id: str
    status: EventStatus
    code: str | None = None


class EventBatchResponse(ProtocolModel):
    """The box removes every listed id from its outbox; ``rejected`` is final."""

    results: list[EventResult]
