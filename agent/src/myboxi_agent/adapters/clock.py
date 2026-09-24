"""System clock with NTP state (SPEC §5.6)."""

from __future__ import annotations

import datetime as dt
import subprocess
import time
from collections.abc import Callable

Runner = Callable[[list[str]], str]
CHECK_EVERY_S = 60.0


def _run(cmd: list[str]) -> str:
    # Fixed argument lists from this module only, no shell.
    return subprocess.run(cmd, capture_output=True, text=True, timeout=5, check=False).stdout  # noqa: S603


class SystemClock:
    def __init__(self, *, assume_trusted: bool = False, runner: Runner = _run) -> None:
        self._assume = assume_trusted
        self._runner = runner
        self._trusted = assume_trusted
        self._checked = -CHECK_EVERY_S

    def monotonic(self) -> float:
        return time.monotonic()

    def now(self) -> dt.datetime:
        return dt.datetime.now(dt.UTC)

    def time_trusted(self) -> bool:
        """``timedatectl``: NTPSynchronized=yes once synced since boot. Sticky once true."""
        if self._trusted:
            return True
        now = time.monotonic()
        if now - self._checked >= CHECK_EVERY_S:
            self._checked = now
            try:
                out = self._runner(["timedatectl", "show", "--property=NTPSynchronized"])
            except (OSError, subprocess.SubprocessError):
                out = ""
            self._trusted = out.strip() == "NTPSynchronized=yes"
        return self._trusted
