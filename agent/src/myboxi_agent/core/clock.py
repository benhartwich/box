"""Injected time (CLAUDE.md rule 3, SPEC §5.6)."""

from __future__ import annotations

import datetime as dt
from typing import Protocol


class Clock(Protocol):
    def monotonic(self) -> float:
        """Seconds, never going backwards; for durations and timers."""
        ...

    def now(self) -> dt.datetime:
        """Aware wall-clock time in UTC. Only meaningful if ``time_trusted()``."""
        ...

    def time_trusted(self) -> bool:
        """True after a successful NTP sync since boot (SPEC §5.6)."""
        ...


class FakeClock:
    """Deterministic clock for tests and simulation."""

    def __init__(
        self,
        now: dt.datetime | None = None,
        *,
        trusted: bool = True,
        monotonic: float = 1000.0,
    ) -> None:
        self._now = now or dt.datetime(2026, 9, 24, 12, 0, tzinfo=dt.UTC)
        self._mono = monotonic
        self.trusted = trusted

    def monotonic(self) -> float:
        return self._mono

    def now(self) -> dt.datetime:
        return self._now

    def time_trusted(self) -> bool:
        return self.trusted

    def advance(self, seconds: float) -> None:
        self._mono += seconds
        self._now += dt.timedelta(seconds=seconds)

    def set_wall(self, now: dt.datetime) -> None:
        self._now = now
