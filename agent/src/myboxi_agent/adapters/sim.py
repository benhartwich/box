"""Simulated hardware for ``myboxi-agent run --sim`` (CLAUDE.md rule 2): figures, buttons,
player and prompts are driven through the control socket and logged."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable, Sequence

from myboxi_agent.adapters.base import ButtonEvent, Placed, ReaderEvent, Removed
from myboxi_agent.adapters.health import Health
from myboxi_agent.core.model import PlanItem, ResumePoint
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
        self, items: Sequence[PlanItem], index: int, position_ms: int, repeat: RepeatMode
    ) -> None:
        self.sources, self.index, self.repeat = [i.source for i in items], index, repeat
        self._base_ms, self._since, self.state = position_ms, time.monotonic(), "playing"
        item = items[index] if items else None
        log.info(
            "play",
            extra={
                "item": item.source if item else None,
                "at_ms": position_ms,
                "gain_db": item.gain_db if item else None,
            },
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


class SimSpotify:
    """Spotify without Soloist (SPEC v0.9 §8.1): every context has ``TRACKS`` titles; the
    control socket plays the Spotify app (``sim spotify play|pause|volume``)."""

    TRACKS = 10

    def __init__(self) -> None:
        self.uri: str | None = None
        self.index = 0
        self.repeat: RepeatMode = "off"
        self.state = "stopped"
        self.volume = 0
        self._base_ms = 0
        self._since = 0.0
        self.logged_in = True
        self.on_playlist_finished: Callable[[], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        self.on_remote_playing: Callable[[bool], None] | None = None
        self.on_external_started: Callable[[], None] | None = None
        self.on_external_volume: Callable[[int], None] | None = None

    def connected(self) -> bool:
        return True

    def playing(self) -> bool:
        return self.state == "playing"

    def _pos_ms(self) -> int:
        if self.state != "playing":
            return self._base_ms
        return self._base_ms + int((time.monotonic() - self._since) * 1000)

    def play_context(self, uri: str, start: ResumePoint, shuffle: bool, repeat: RepeatMode) -> None:
        self.uri, self.repeat = uri, repeat
        self.index = min(start.item_index, self.TRACKS - 1)
        self._base_ms, self._since, self.state = start.position_ms, time.monotonic(), "playing"
        log.info(
            "spotify play", extra={"uri": uri, "track": self.index, "at_ms": start.position_ms}
        )

    def skip(self) -> None:
        if self.index + 1 < self.TRACKS or self.repeat == "all":
            self.index = (self.index + 1) % self.TRACKS
            self._base_ms, self._since = 0, time.monotonic()
            log.info("spotify next", extra={"track": self.index})
            return
        self.state = "stopped"
        log.info("spotify context ended (the guard would stop autoplay here)")
        if self.on_playlist_finished is not None:
            self.on_playlist_finished()

    def pause(self) -> None:
        self._base_ms, self.state = self._pos_ms(), "paused"
        log.info("spotify pause")

    def resume(self) -> None:
        self._since, self.state = time.monotonic(), "playing"
        log.info("spotify resume")

    def stop(self) -> None:
        if self.state == "playing":
            self.pause()
        self.uri = None

    def set_volume(self, volume: int) -> None:
        self.volume = volume
        log.info("spotify volume", extra={"volume": volume})

    def position(self) -> ResumePoint | None:
        if self.uri is None:
            return None
        return ResumePoint(self.index, self._pos_ms(), f"spotify:track:sim{self.index:019d}")

    # --- the Spotify app ------------------------------------------------------------------

    def app(self, action: str, value: int = 0) -> None:
        match action:
            case "play":
                self.uri = self.uri or "spotify:playlist:sim"
                self._since, self.state = time.monotonic(), "playing"
                if self.on_remote_playing is not None:
                    self.on_remote_playing(True)
            case "pause":
                self.pause()
                if self.on_remote_playing is not None:
                    self.on_remote_playing(False)
            case "volume":
                self.volume = value
                if self.on_external_volume is not None:
                    self.on_external_volume(value)
            case _:
                raise ValueError(action)


class SimSpotifyService:
    """``SoloistService`` without systemd: ready whenever Spotify is enabled."""

    def __init__(self, player: SimSpotify, enabled: Callable[[], bool]) -> None:
        self.player = player
        self.enabled = enabled

    async def run(self) -> None:
        return

    def trigger(self) -> None:
        pass

    def key_changed(self) -> None:
        pass

    def unavailable(self) -> str | None:
        return None

    def status(self) -> dict[str, object] | None:
        if not self.enabled():
            return None
        return {"installed": True, "state": "ready", "logged_in": True, "device_name": "Myboxi Sim"}


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
