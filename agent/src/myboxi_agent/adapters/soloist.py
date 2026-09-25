"""Spotify through Soloist's local WebSocket API (SPEC v0.9 §8.1; §10: 127.0.0.1 only).

``SoloistClient`` keeps the connection: commands are only sent while connected (a queued
``play`` must never start music minutes later), events go to a callback. ``SoloistPlayer``
plays a figure's context and enforces the box rules on everything Soloist plays:

- resume: muted ``play`` → ``pause`` → N × ``skip_next`` → check the track URI → ``seek``;
- the guard against catalog drift: autoplay titles end the content;
- explicit titles are skipped unless the box allows them;
- actions from the Spotify app (play, pause, volume, another context) go to the controller.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, cast

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from myboxi_agent.core.clock import Clock
from myboxi_agent.core.model import ResumePoint
from myboxi_protocol.state import RepeatMode

log = logging.getLogger(__name__)

WS_HOST = "127.0.0.1"  # SPEC §10: never another address
STEP_TIMEOUT_S = 3.0
MAX_RESUME_SKIPS = 50
MAX_EXPLICIT_SKIPS = 10
VOLUME_TOLERANCE = 1  # Soloist may round what it reports back
EXPECT_S = 5.0  # a status we caused must arrive within this time, else it is the app's
RECONNECT_S = 1.0

Event = dict[str, Any]


class SoloistClient:
    def __init__(
        self,
        port: int,
        on_event: Callable[[Event], None],
        on_connection: Callable[[bool], None] | None = None,
    ) -> None:
        self.url = f"ws://{WS_HOST}:{port}"
        self.on_event = on_event
        self.on_connection = on_connection
        self.connected = asyncio.Event()
        self._queue: asyncio.Queue[str] = asyncio.Queue()

    def send(self, command: str, **fields: Any) -> bool:
        if not self.connected.is_set():
            return False
        self._queue.put_nowait(json.dumps({"type": "command", "command": command, **fields}))
        return True

    async def run(self) -> None:
        while True:
            try:
                async with connect(
                    self.url, open_timeout=5, proxy=None, compression=None, max_size=4 * 1024**2
                ) as ws:
                    while not self._queue.empty():
                        self._queue.get_nowait()  # stale commands from an earlier connection
                    self.connected.set()
                    self._notify(True)
                    await self._session(ws)
            except (OSError, TimeoutError, WebSocketException):
                pass
            finally:
                if self.connected.is_set():
                    self.connected.clear()
                    self._notify(False)
            await asyncio.sleep(RECONNECT_S)

    async def _session(self, ws: ClientConnection) -> None:
        async def writer() -> None:
            while True:
                await ws.send(await self._queue.get())

        task = asyncio.create_task(writer())
        try:
            async for raw in ws:
                try:
                    message = json.loads(raw)
                except ValueError:
                    continue
                if isinstance(message, dict):
                    event = cast(Event, message)
                    if isinstance(event.get("type"), str):
                        self.on_event(event)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, WebSocketException):
                await task

    def _notify(self, up: bool) -> None:
        if self.on_connection is not None:
            self.on_connection(up)


class SoloistError(Exception):
    pass


@dataclass
class _Figure:
    uri: str
    context: str | None = None  # as Soloist reports it (a track URI may play in its album)
    first_track: str | None = None
    index: int = 0
    started: bool = False
    explicit_skips: int = 0
    tracks_left: int | None = None  # context titles still queued, from queue_changed


def _uri(entity: Any) -> str | None:
    if isinstance(entity, dict):
        uri = entity.get("uri")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
        return uri if isinstance(uri, str) else None
    return None


def is_explicit(item: Any) -> bool:
    """``decorations.playback.content_ratings`` contains ``explicit`` (SPEC v0.9 §8.1)."""
    try:
        ratings = item["decorations"]["playback"]["content_ratings"]
    except (KeyError, TypeError):
        return False
    return isinstance(ratings, list) and "explicit" in ratings


def repeat_commands(repeat: RepeatMode) -> list[tuple[str, bool]]:
    """Soloist's order for the repeat modes (WebSocket reference, "Repeat Mode Coordination")."""
    match repeat:
        case "off":
            return [("set_repeat_track", False), ("set_repeat_context", False)]
        case "all":
            return [("set_repeat_track", False), ("set_repeat_context", True)]
        case "one":
            return [("set_repeat_context", False), ("set_repeat_track", True)]


class SoloistPlayer:
    """The context half of ``core.ports.Player``; ``RoutingPlayer`` combines it with mpv."""

    def __init__(
        self,
        client_factory: Callable[[Callable[[Event], None], Callable[[bool], None]], SoloistClient],
        clock: Clock,
        allow_explicit: Callable[[], bool],
    ) -> None:
        self.client = client_factory(self._on_event, self._on_connection)
        self.clock = clock
        self.allow_explicit = allow_explicit
        self.on_playlist_finished: Callable[[], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        self.on_remote_playing: Callable[[bool], None] | None = None
        self.on_external_started: Callable[[], None] | None = None
        self.on_external_volume: Callable[[int], None] | None = None
        self.logged_in = False
        self.is_active = False
        self.device_name: str | None = None
        self.status = "idle"
        self.item: Event | None = None
        self.context_uri: str | None = None
        self.figure: _Figure | None = None
        self._pos_ms = 0
        self._pos_at = 0.0
        self._speed = 1.0
        self._upcoming: list[tuple[str, str]] = []
        self._expected: list[tuple[str, float]] = []  # statuses our own commands cause
        self._volume: int | None = None  # what the volume policy wants
        self._sent_volume: int | None = None  # what Soloist was told last (0 while muted)
        self._busy = False
        self._task: asyncio.Task[None] | None = None
        self._waiters: list[tuple[str, asyncio.Future[Event]]] = []

    # --- state ------------------------------------------------------------------------------

    def connected(self) -> bool:
        return self.client.connected.is_set()

    def ready(self) -> bool:
        return self.connected() and self.logged_in

    def playing(self) -> bool:
        return self.status in ("playing", "buffering")

    def position_ms(self) -> int:
        if self.status == "playing":
            elapsed = (self.clock.monotonic() - self._pos_at) * 1000 * self._speed
            return max(0, int(self._pos_ms + elapsed))
        return self._pos_ms

    # --- core.ports.Player (context part) ---------------------------------------------------

    def play_context(self, uri: str, start: ResumePoint, shuffle: bool, repeat: RepeatMode) -> None:
        if not self.connected():
            self._error("not_running")
            return
        if not self.logged_in:
            self._error("not_logged_in")
            return
        self._cancel_task()
        self.figure = _Figure(uri)
        self._task = asyncio.get_running_loop().create_task(
            self._start(self.figure, start, shuffle, repeat)
        )

    def skip(self) -> None:
        self.client.send("skip_next")

    def pause(self) -> None:
        self._expect_status("paused")
        self.client.send("pause")

    def resume(self) -> None:
        self._expect_status("playing")
        self.client.send("play")

    def stop(self) -> None:
        self._cancel_task()
        self.figure = None
        if self.playing():
            self.pause()

    def set_volume(self, volume: int) -> None:
        self._volume = max(0, min(volume, 100))
        if not self._busy:
            self._send_volume(self._volume)

    def position(self) -> ResumePoint | None:
        f = self.figure
        if f is None:
            return None
        return ResumePoint(f.index, self.position_ms(), _uri(self.item))

    # --- starting a figure ------------------------------------------------------------------

    async def _start(
        self, figure: _Figure, start: ResumePoint, shuffle: bool, repeat: RepeatMode
    ) -> None:
        resume = start.item_index > 0 or start.position_ms > 0
        self._busy = True
        try:
            if not self.is_active:
                self.client.send("activate")
            if resume:
                self._send_volume(0)  # nobody hears the skipping
            await self._play(figure)
            self.client.send("set_shuffle", enabled=shuffle)
            for command, enabled in repeat_commands(repeat):
                self.client.send(command, enabled=enabled)
            if resume and not await self._seek_to(figure, start):
                log.info("spotify resume failed, starting over")
                await self._play(figure)
            figure.started = True
        except (TimeoutError, SoloistError) as exc:
            if self.figure is figure:
                self.figure = None
                log.warning("spotify start failed", extra={"error": str(exc)[:200]})
                self._error("soloist_error")
        finally:
            self._busy = False
            if self._volume is not None:
                self._send_volume(self._volume)

    async def _play(self, figure: _Figure) -> None:
        self._expect_status("playing")
        self.client.send("play", uri=figure.uri)
        item = await self._wait("track_changed")
        figure.index = 0
        figure.first_track = _uri(item.get("item"))
        figure.context = self.context_uri or figure.uri
        if self.status != "playing":
            self._expect_status("playing")
            self.client.send("play")

    async def _seek_to(self, figure: _Figure, start: ResumePoint) -> bool:
        if start.item_index > MAX_RESUME_SKIPS:
            return False
        if start.item_index > 0:
            self._expect_status("paused")
            self.client.send("pause")
            for _ in range(start.item_index):
                self.client.send("skip_next")
                await self._wait("track_changed")
            if start.item_key is not None and _uri(self.item) != start.item_key:
                return False
            figure.index = start.item_index
        if start.position_ms > 0:
            self.client.send("seek", position_ms=start.position_ms)
            self._pos_ms, self._pos_at = start.position_ms, self.clock.monotonic()
        if self._volume is not None:
            self._send_volume(self._volume)
        self._expect_status("playing")
        self.client.send("play")
        return True

    async def _wait(self, event_type: str) -> Event:
        future: asyncio.Future[Event] = asyncio.get_running_loop().create_future()
        self._waiters.append((event_type, future))
        try:
            return await asyncio.wait_for(future, STEP_TIMEOUT_S)
        finally:
            self._waiters = [(t, f) for t, f in self._waiters if f is not future]

    def _cancel_task(self) -> None:
        if self._task is not None and not self._task.done():
            self._task.cancel()
        self._task = None
        self._busy = False

    # --- events -----------------------------------------------------------------------------

    def _on_connection(self, up: bool) -> None:
        if up:
            self.client.send("get_auth_state")
            if self._volume is not None:
                self._send_volume(self._volume)
            return
        self.logged_in = False
        self.status = "idle"
        if self.figure is not None:
            self.figure = None
            self._error("not_running")

    def _on_event(self, event: Event) -> None:
        kind = event["type"]
        for waited, future in list(self._waiters):
            if kind == "error" and not future.done():
                future.set_exception(SoloistError(str(event.get("message", ""))[:200]))
            elif waited == kind and not future.done():
                future.set_result(event)
        match kind:
            case "auth_state" | "device_changed":
                if isinstance(event.get("logged_in"), bool):
                    was, self.logged_in = self.logged_in, event["logged_in"]
                    if self.logged_in and not was and self._volume is not None:
                        # Soloist refuses commands before the login: the volume only now.
                        self._send_volume(self._volume)
                self.is_active = bool(event.get("is_active", self.is_active))
                if isinstance(event.get("device_name"), str):
                    self.device_name = event["device_name"][:64]
            case "playback_state":
                self._snapshot(event)
            case "track_changed":
                self._track_changed(event.get("item"))
            case "context_changed":
                self._context_changed(_uri(event.get("context")))
            case "playback_changed":
                self._status_changed(str(event.get("status", "")))
            case "volume_changed":
                self._volume_changed(event.get("volume"))
            case "position_sync":
                self._position(event.get("position"))
            case "queue_changed":
                self._queue_changed(event.get("upcoming"))
            case "error":
                message = str(event.get("message", ""))[:200]
                if "authentication" in message:
                    self.logged_in = False
                log.warning("soloist error", extra={"message": message})
            case _:
                pass

    def _snapshot(self, event: Event) -> None:
        if isinstance(event.get("status"), str):
            self.status = event["status"]
        if isinstance(event.get("item"), dict):
            self.item = event["item"]
        self.context_uri = _uri(event.get("context")) or self.context_uri
        self._position(event.get("position"))
        if isinstance(event.get("volume"), int) and not self._busy:
            self._volume_changed(event["volume"])

    def _position(self, position: Any) -> None:
        if isinstance(position, dict):
            ms = position.get("position_ms")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            speed = position.get("speed", 1.0)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
            if isinstance(ms, int):
                self._pos_ms, self._pos_at = ms, self.clock.monotonic()
            if isinstance(speed, int | float):
                self._speed = float(speed)

    def _queue_changed(self, upcoming: Any) -> None:
        entries: list[tuple[str, str]] = []
        if isinstance(upcoming, list):
            for entry in upcoming:  # pyright: ignore[reportUnknownVariableType]
                if isinstance(entry, dict):
                    uri = _uri(entry.get("item"))  # pyright: ignore[reportUnknownMemberType, reportUnknownArgumentType]
                    source = entry.get("source")  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
                    if uri and isinstance(source, str):
                        entries.append((uri, source))
        self._upcoming = entries
        f = self.figure
        if f is not None and (f.context is None or self.context_uri == f.context):
            f.tracks_left = sum(1 for _, source in entries if source == "context")

    def _track_changed(self, item: Any) -> None:
        previous = _uri(self.item)
        self.item = item if isinstance(item, dict) else None
        self._pos_ms, self._pos_at = 0, self.clock.monotonic()
        uri = _uri(item)
        if self._busy or uri is None:
            return
        source = next((s for u, s in self._upcoming if u == uri), None)
        if source == "autoplay":
            self._guard("autoplay")
            return
        f = self.figure
        if f is not None and f.started and uri != previous:
            f.index = 0 if uri == f.first_track else f.index + 1
        if is_explicit(item) and not self.allow_explicit():
            skips = f.explicit_skips + 1 if f is not None else 0
            if f is not None:
                f.explicit_skips = skips
            if skips >= MAX_EXPLICIT_SKIPS:
                self._guard("explicit")
                return
            self.client.send("skip_next")
            return
        if f is not None:
            f.explicit_skips = 0

    def _context_changed(self, uri: str | None) -> None:
        self.context_uri = uri
        f = self.figure
        if self._busy or f is None or not f.started or uri is None or uri in (f.context, f.uri):
            return
        if f.tracks_left == 0:
            self._guard("autoplay")  # the context had ended: this is Spotify's drift
            return
        # Someone chose something else in the Spotify app: the figure's session ends with its
        # position saved (position() still describes the figure), the new one is external.
        if self.on_external_started is not None:
            self.on_external_started()
        self.figure = None

    def _expect_status(self, status: str) -> None:
        now = time.monotonic()
        self._expected = [(s, t) for s, t in self._expected if now - t < EXPECT_S][-7:]
        self._expected.append((status, now))

    def _ours(self, status: str) -> bool:
        """Consume the expectation this status fulfils (and older ones before it)."""
        now = time.monotonic()
        for i, (expected, at) in enumerate(self._expected):
            if expected == status and now - at < EXPECT_S:
                del self._expected[: i + 1]
                return True
        return False

    def _status_changed(self, status: str) -> None:
        old, self.status = self.status, status
        if status == "buffering":
            return
        ours = self._ours(status)
        if ours or self._busy:
            return
        if status == "idle":
            if self.figure is not None and self.figure.started and old != "idle":
                self.figure = None  # the context ran out (repeat off, no autoplay)
                if self.on_playlist_finished is not None:
                    self.on_playlist_finished()
            elif self.on_remote_playing is not None:
                self.on_remote_playing(False)
            return
        if status == "playing":
            self._pos_at = self.clock.monotonic()
        if self.on_remote_playing is not None and status in ("playing", "paused"):
            self.on_remote_playing(status == "playing")

    def _volume_changed(self, value: Any) -> None:
        if not isinstance(value, int) or self._busy:
            return
        sent = self._sent_volume
        if sent is not None and abs(value - sent) <= VOLUME_TOLERANCE:
            return
        self._sent_volume = value
        if self.on_external_volume is not None:
            self.on_external_volume(value)  # the policy decides and sets it back if needed

    def _guard(self, reason: str) -> None:
        """SPEC v0.9 §8.1: stop drift into titles nobody bound to the figure."""
        log.info("spotify guard", extra={"reason": reason})
        self._expect_status("paused")
        self.client.send("pause")
        figure, self.figure = self.figure, None
        if reason == "explicit" and figure is not None:
            self._error("explicit")
        elif figure is not None:
            if self.on_playlist_finished is not None:
                self.on_playlist_finished()
        elif self.on_remote_playing is not None:
            self.on_remote_playing(False)

    def _send_volume(self, volume: int) -> None:
        self._sent_volume = volume
        self.client.send("set_volume", volume=volume)

    def _error(self, code: str) -> None:
        if self.on_error is not None:
            self.on_error(code)
