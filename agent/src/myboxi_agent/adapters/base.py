"""Input adapter interfaces; output interfaces live in ``core.ports``."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class Placed:
    uid: str


@dataclass(frozen=True)
class Removed:
    pass


ReaderEvent = Placed | Removed


@dataclass(frozen=True)
class ButtonEvent:
    button: str
    pressed: bool


class Reader(Protocol):
    def events(self) -> AsyncIterator[ReaderEvent]: ...


class Buttons(Protocol):
    def events(self) -> AsyncIterator[ButtonEvent]: ...


class PlayerCallbacks(Protocol):
    """Set by the app so the player can report back to the controller."""

    on_playlist_finished: Callable[[], None] | None
    on_error: Callable[[str], None] | None
