"""Keeps Soloist running while Spotify is wanted (SPEC v0.9 §8.1, §6.4).

Soloist is the user unit ``myboxi-soloist.service`` next to the agent (same user session, so
``systemctl --user`` needs no polkit). This loop starts it when Spotify is enabled and a key
is stored, stops it otherwise, asks the update unit for a build when none is installed or the
installed one expired, and restarts Soloist onto a new build only while Spotify is silent.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import subprocess
from collections.abc import Callable
from typing import Any, Protocol

from myboxi_agent.adapters.soloist import SoloistPlayer
from myboxi_agent.core.clock import Clock
from myboxi_agent.soloist.install import SoloistPaths, read_state

log = logging.getLogger(__name__)

UNIT = "myboxi-soloist.service"
UPDATE_UNIT = "myboxi-soloist-update.service"
CHECK_S = 30.0
INSTALL_RETRY_S = 15 * 60
EXPIRED_RETRY_S = 60 * 60


class Units(Protocol):
    def run(self, verb: str, unit: str) -> bool: ...


class SystemdUserUnits:
    def run(self, verb: str, unit: str) -> bool:
        args = ["systemctl", "--user", verb, unit]
        if verb == "start" and unit == UPDATE_UNIT:
            args.insert(2, "--no-block")
        try:
            # Fixed argument lists only, no shell.
            done = subprocess.run(args, check=False, capture_output=True, timeout=60)  # noqa: S603
        except (OSError, subprocess.TimeoutExpired):
            return False
        return done.returncode == 0


class SoloistService:
    def __init__(
        self,
        *,
        paths: SoloistPaths,
        player: SoloistPlayer,
        clock: Clock,
        enabled: Callable[[], bool],
        key_set: Callable[[], bool],
        device_name: str,
        units: Units | None = None,
    ) -> None:
        self.paths = paths
        self.player = player
        self.clock = clock
        self.enabled = enabled
        self.key_set = key_set
        self.device_name = device_name
        self.units = units or SystemdUserUnits()
        self._wake = asyncio.Event()
        self._running: str | None = None  # release Soloist was started with
        self._restart = False
        self._update_asked = -1e18

    def trigger(self) -> None:
        self._wake.set()

    def key_changed(self) -> None:
        """A new key takes effect with a restart (it is a command line argument)."""
        self._restart = True
        self._wake.set()

    async def run(self) -> None:
        while True:
            try:
                await self.reconcile()
            except Exception:
                log.exception("soloist supervision failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), CHECK_S)
            self._wake.clear()

    async def _unit(self, verb: str, unit: str = UNIT) -> bool:
        return await asyncio.to_thread(self.units.run, verb, unit)

    async def reconcile(self) -> None:
        # Database reads stay in the event loop; only systemctl runs in a thread.
        wanted = self.enabled() and self.key_set()
        state = read_state(self.paths)
        installed = self.paths.binary.is_file() and state.release is not None
        if not wanted:
            if self._running is not None or await self._unit("is-active"):
                await self._unit("stop")
                log.info("spotify off: soloist stopped")
            self._running = None
            return
        if not installed or state.expired:
            if state.expired and await self._unit("is-active"):
                await self._unit("stop")
            self._running = None
            retry = EXPIRED_RETRY_S if state.expired else INSTALL_RETRY_S
            now = self.clock.monotonic()
            if now - self._update_asked >= retry:
                self._update_asked = now
                await self._unit("start", UPDATE_UNIT)
                log.info("soloist update requested", extra={"expired": state.expired})
            return
        release = self.paths.current_release()
        if not await self._unit("is-active"):
            await self._unit("start")
            self._running, self._restart = release, False
            return
        if self._running is None:
            self._running = release  # the agent restarted while Soloist kept running
        if (self._restart or release != self._running) and not self.player.playing():
            await self._unit("restart")
            self._running, self._restart = release, False
            log.info("soloist restarted", extra={"release": release})

    # --- state ------------------------------------------------------------------------------

    def unavailable(self) -> str | None:
        """Why a Spotify figure cannot play now (SPEC v0.9 §6.5), or None."""
        if not self.key_set():
            return "not_configured"
        state = read_state(self.paths)
        if state.expired:
            return "expired"
        if not self.paths.binary.is_file() or not self.player.connected():
            return "not_running"
        if not self.player.logged_in:
            return "not_logged_in"
        return None

    def status(self) -> dict[str, Any] | None:
        """``reported.soloist`` (SPEC v0.9 §6.4)."""
        state = read_state(self.paths)
        installed = self.paths.binary.is_file() and state.release is not None
        enabled = self.enabled()
        if not enabled and not installed:
            return None
        status: dict[str, Any] = {"installed": installed}
        if state.build_expires_at:
            status["build_expires_at"] = state.build_expires_at
        if enabled:
            status["device_name"] = self.device_name
            if not self.key_set():
                status["state"] = "no_key"
            elif state.expired:
                status["state"] = "expired"
            elif not installed:
                status["state"] = "failed" if state.error else "installing"
            elif self.player.connected():
                status["state"] = "ready"
                status["logged_in"] = self.player.logged_in
            else:
                status["state"] = "starting"
        return status
