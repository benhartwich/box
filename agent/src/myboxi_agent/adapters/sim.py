"""Simulated hardware for ``myboxi-agent run --sim`` (CLAUDE.md rule 2): figures, buttons,
player and prompts are driven through the control socket and logged."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence

from myboxi_agent.adapters.base import ButtonEvent, Placed, ReaderEvent, Removed
from myboxi_agent.adapters.health import Health
from myboxi_agent.core.model import ResumePoint
from myboxi_protocol.state import RepeatMode

log = logging.getLogger("myboxi_agent.sim")


class SimReader:
    def __init__(self, health: Health | None = None) -> None:
        self._queue: asyncio.Queue[ReaderEvent] = asyncio.Queue()
        self.current: str | None = None
        self.health = health or Health()
        self.health.ok("nfc")

    def fail(self, code: str = "not_responding") -> None:
        """Simulates a broken reader for the self-test (SPEC v0.6 §6.4)."""
        self.health.set("nfc", "fail", code)

    def recover(self) -> None:
        self.health.ok("nfc")

    def place(self, uid: str) -> None:
        self.current = uid
        self._queue.put_nowait(Placed(uid))

    def remove(self) -> None:
        self.current = None
        self._queue.put_nowait(Removed())

    async def events(self) -> AsyncIterator[ReaderEvent]:
        while True:
            yield await self._queue.get()


class SimButtons:
    def __init__(self) -> None:
        self._queue: asyncio.Queue[ButtonEvent] = asyncio.Queue()

    async def hold(self, buttons: Sequence[str], seconds: float) -> None:
        for b in buttons:
            self._queue.put_nowait(ButtonEvent(b, pressed=True))
        await asyncio.sleep(seconds)
        for b in buttons:
            self._queue.put_nowait(ButtonEvent(b, pressed=False))

    async def events(self) -> AsyncIterator[ButtonEvent]:
        while True:
            yield await self._queue.get()


class SimPlayer:
    """Tracks what a player would do; position advances with real time while playing."""

    def __init__(self) -> None:
        self.sources: list[str] = []
        self.index = 0
        self.repeat: RepeatMode = "off"
        self.state = "stopped"
        self.volume = 0
        self._base_ms = 0
        self._since = 0.0
        self.on_playlist_finished: Callable[[], None] | None = None
        self.on_error: Callable[[str], None] | None = None

    def _pos_ms(self) -> int:
        if self.state != "playing":
            return self._base_ms
        return self._base_ms + int((time.monotonic() - self._since) * 1000)

    def play(
        self, sources: Sequence[str], index: int, position_ms: int, repeat: RepeatMode
    ) -> None:
        self.sources, self.index, self.repeat = list(sources), index, repeat
        self._base_ms, self._since, self.state = position_ms, time.monotonic(), "playing"
        log.info(
            "play", extra={"item": self.sources[index] if sources else None, "at_ms": position_ms}
        )

    def pause(self) -> None:
        self._base_ms, self.state = self._pos_ms(), "paused"
        log.info("pause", extra={"at_ms": self._base_ms})

    def resume(self) -> None:
        self._since, self.state = time.monotonic(), "playing"
        log.info("resume")

    def stop(self) -> None:
        self.state, self._base_ms = "stopped", 0
        log.info("stop")

    def set_volume(self, volume: int) -> None:
        self.volume = volume
        log.info("volume", extra={"volume": volume})

    def position(self) -> ResumePoint | None:
        if not self.sources:
            return None
        return ResumePoint(self.index, self._pos_ms())

    def finish(self) -> None:
        """Sim command: the playlist reached its end."""
        if self.on_playlist_finished is not None and self.repeat == "off":
            self.on_playlist_finished()


class SimAnnouncer:
    def __init__(self) -> None:
        self.history: list[tuple[str, ...]] = []

    def announce(self, *prompts: str) -> None:
        self.history.append(tuple(str(p) for p in prompts))
        log.info("announce", extra={"prompts": [str(p) for p in prompts]})


class SimSystem:
    def __init__(self) -> None:
        self.on_repair: Callable[[], None] | None = None
        self.setup_requested = 0

    def request_setup_mode(self) -> None:
        self.setup_requested += 1
        log.info("setup mode requested (sim: no access point)")

    def request_repair(self) -> None:
        if self.on_repair is not None:
            self.on_repair()
