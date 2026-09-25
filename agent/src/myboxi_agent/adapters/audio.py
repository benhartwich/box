"""Audio self-test: both mpv instances reachable and PipeWire has an output (SPEC v0.6 §6.4)."""

from __future__ import annotations

import asyncio
import subprocess
from collections.abc import Callable, Sequence
from typing import Protocol

from myboxi_agent.adapters.health import Health

CHECK_EVERY_S = 60.0
STARTUP_GRACE_S = 15.0


class Connectable(Protocol):
    connected: asyncio.Event


def pipewire_sinks() -> list[str] | None:
    """Output devices from ``wpctl status``; None when PipeWire does not answer."""
    try:
        out = subprocess.run(
            ["wpctl", "status"],  # noqa: S607 - fixed command from the image
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    return parse_sinks(out)


def parse_sinks(wpctl_status: str) -> list[str] | None:
    if "Sinks:" not in wpctl_status:
        return None
    section = wpctl_status.split("Sinks:", 1)[1].split("Sources:", 1)[0]
    return [line.strip(" │*") for line in section.splitlines() if "." in line]


class AudioMonitor:
    def __init__(
        self,
        health: Health,
        clients: Sequence[Connectable],
        sinks: Callable[[], list[str] | None] = pipewire_sinks,
        every_s: float = CHECK_EVERY_S,
        grace_s: float = STARTUP_GRACE_S,
    ) -> None:
        self.health = health
        self.clients = list(clients)
        self.sinks = sinks
        self.every_s = every_s
        self.grace_s = grace_s

    async def check(self) -> None:
        if not all(c.connected.is_set() for c in self.clients):
            self.health.set("audio", "fail", "player_down")
            return
        if await asyncio.to_thread(self.sinks):
            self.health.ok("audio")
        else:
            self.health.set("audio", "fail", "no_output")

    async def run(self) -> None:
        await asyncio.sleep(self.grace_s)  # mpv and PipeWire start with the agent
        while True:
            await self.check()
            await asyncio.sleep(self.every_s)
