"""Interfaces the core drives. Adapters implement them (real, sim, fakes in tests).

Calls are commands: they return immediately; adapters do the work asynchronously and report
back through Controller methods (e.g. ``track_ended``).
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Literal, Protocol

from pydantic import BaseModel

from myboxi_agent.core.model import Resolution, ResumePoint
from myboxi_protocol.state import RepeatMode

EventType = Literal[
    "token_unknown",
    "token_played",
    "playback_error",
    "storage_full",
    "sync_error",
    "resume_position",
]


class Player(Protocol):
    def play(
        self, sources: Sequence[str], index: int, position_ms: int, repeat: RepeatMode
    ) -> None:
        """Play the playlist from ``index``; ``repeat`` loops one item or the whole list.
        Without repeat the adapter calls ``Controller.playlist_finished`` at the end."""
        ...

    def pause(self) -> None: ...

    def resume(self) -> None: ...

    def stop(self) -> None: ...

    def set_volume(self, volume: int) -> None: ...

    def position(self) -> ResumePoint | None:
        """Last known (item index within the playlist given to ``play``, position)."""
        ...


class Announcer(Protocol):
    def announce(self, *prompts: str) -> None:
        """Play prompts in order; a missing prompt plays the error tone (never silence)."""
        ...


class Outbox(Protocol):
    def emit(self, event_type: EventType, data: BaseModel) -> None: ...


class ResumeStore(Protocol):
    def get(self, token_id: uuid.UUID) -> ResumePoint | None: ...

    def save(self, token_id: uuid.UUID, point: ResumePoint) -> None: ...


class Library(Protocol):
    def resolve(self, uid: str) -> Resolution: ...


class System(Protocol):
    def request_setup_mode(self) -> None: ...

    def request_repair(self) -> None: ...
