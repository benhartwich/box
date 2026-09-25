"""Setup phase after pairing (SPEC v0.6 §9.6).

For 60 minutes after a successful pairing the box syncs and reports faster, collects the
buttons pressed for the web wizard's button test and acknowledges each press with a tone.
Measured from ``paired_at`` (wall clock, so it survives a reboot while the time is trusted)
and, right after pairing in this boot, from the monotonic clock.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from myboxi_agent.core.clock import Clock

SETUP_PHASE_S = 60 * 60


class SetupPhase:
    def __init__(self, clock: Clock, paired_at: Callable[[], dt.datetime | None]) -> None:
        self.clock = clock
        self.paired_at = paired_at
        self.seen: set[str] = set()
        self._started_mono: float | None = None

    def started(self) -> None:
        """Pairing succeeded just now."""
        self._started_mono = self.clock.monotonic()
        self.seen = set()

    def active(self) -> bool:
        paired_at = self.paired_at()
        if paired_at is None:  # unpaired, e.g. after re-pairing was requested
            return False
        if self._started_mono is not None:
            return self.clock.monotonic() - self._started_mono < SETUP_PHASE_S
        if not self.clock.time_trusted():
            return False
        return 0 <= (self.clock.now() - paired_at).total_seconds() < SETUP_PHASE_S

    def record(self, button: str) -> bool:
        """Remembers a pressed button for ``button_test``; False outside the phase."""
        if not self.active():
            return False
        self.seen.add(button)
        return True
