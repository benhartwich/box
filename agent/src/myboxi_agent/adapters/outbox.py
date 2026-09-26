"""Outbox port: events as SPEC §6.0 envelopes, persisted until the server confirms them."""

from __future__ import annotations

import uuid
from collections.abc import Callable
from pathlib import Path

from pydantic import BaseModel

from myboxi_agent.core.clock import Clock
from myboxi_agent.core.ports import EventType
from myboxi_agent.ids import ulid
from myboxi_agent.store.repos import OutboxRepo
from myboxi_protocol.events import event_adapter


class EventOutbox:
    def __init__(self, repo: OutboxRepo, clock: Clock, boot_id: uuid.UUID) -> None:
        self.repo = repo
        self.clock = clock
        self.boot_id = boot_id
        self.on_emit: Callable[[], None] | None = None  # e.g. MQTT sends at once

    def emit(self, event_type: EventType, data: BaseModel) -> None:
        event_id = ulid()
        envelope = {
            "v": 1,
            "id": event_id,
            "ts": self.clock.now().isoformat(),
            "type": event_type,
            # SPEC §5.6: boot id and monotonic milliseconds next to the (maybe wrong) wall time.
            "boot_id": str(self.boot_id),
            "mono_ms": int(self.clock.monotonic() * 1000),
            "data": data.model_dump(mode="json"),
        }
        event_adapter.validate_python(envelope)  # never queue something the server rejects
        self.repo.add(event_id, envelope)
        if self.on_emit is not None:
            self.on_emit()


def read_boot_id() -> uuid.UUID:
    try:
        return uuid.UUID(Path("/proc/sys/kernel/random/boot_id").read_text("ascii").strip())
    except (OSError, ValueError):
        return uuid.uuid4()
