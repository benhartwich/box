"""Message envelope (SPEC §6.0)."""

from __future__ import annotations

from typing import Any, Literal
from uuid import UUID

from pydantic import AwareDatetime

from box_protocol.common import NonNegativeInt, ProtocolModel, Ulid


class EnvelopeCore(ProtocolModel):
    """Fields every envelope has. Subclasses fix ``type`` and ``data``."""

    v: Literal[1] = 1
    id: Ulid
    ts: AwareDatetime


class EnvelopeBase(EnvelopeCore):
    # SPEC §5.6: boot id and monotonic milliseconds since boot (required for events, §6.5).
    boot_id: UUID | None = None
    mono_ms: NonNegativeInt | None = None


class RawEnvelope(EnvelopeBase):
    """Envelope with untyped payload, e.g. for per-message validation of a batch."""

    id: str  # pyright: ignore[reportIncompatibleVariableOverride]  # validated per message
    type: str
    data: dict[str, Any]
