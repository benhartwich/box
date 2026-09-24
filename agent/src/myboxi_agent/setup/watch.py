"""Starts setup mode on its own (SPEC v0.5 §9.3) and triggers a sync after reconnecting (§5.2)."""

from __future__ import annotations

import asyncio
import logging
import subprocess
from collections.abc import Callable
from typing import Protocol

from myboxi_agent.core.clock import Clock
from myboxi_agent.core.ports import System

log = logging.getLogger(__name__)

CHECK_EVERY_S = 10.0
OFFLINE_GRACE_S = 120.0
RETRIGGER_AFTER_S = 30 * 60


def setup_active() -> bool:
    proc = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", "myboxi-setupd.service"], check=False
    )
    return proc.returncode == 0


class WatchedNetwork(Protocol):
    def online(self) -> bool: ...
    def ethernet_connected(self) -> bool: ...
    def wifi_configured(self) -> bool: ...


class NetworkWatch:
    def __init__(
        self,
        nm: WatchedNetwork,
        system: System,
        clock: Clock,
        on_online: Callable[[], None],
        is_setup_active: Callable[[], bool] = setup_active,
    ) -> None:
        self.nm = nm
        self.system = system
        self.clock = clock
        self.on_online = on_online
        self.is_setup_active = is_setup_active
        self._offline_since: float | None = None
        self._was_online = True
        self._last_trigger = -RETRIGGER_AFTER_S * 2

    async def run(self) -> None:
        while True:
            try:
                await asyncio.to_thread(self.check)
            except Exception:
                log.exception("network watch failed")
            await asyncio.sleep(CHECK_EVERY_S)

    def check(self) -> None:
        now = self.clock.monotonic()
        if self.is_setup_active():
            self._offline_since = None
            return
        if self.nm.online() or self.nm.ethernet_connected():
            if not self._was_online:
                log.info("online again")
                self.on_online()
            self._was_online, self._offline_since = True, None
            return
        self._was_online = False
        if self._offline_since is None:
            self._offline_since = now
        no_wifi = not self.nm.wifi_configured()
        lost = now - self._offline_since >= OFFLINE_GRACE_S
        if (no_wifi or lost) and now - self._last_trigger >= RETRIGGER_AFTER_S:
            self._last_trigger = now
            log.info("starting setup mode", extra={"no_wifi": no_wifi})
            self.system.request_setup_mode()
