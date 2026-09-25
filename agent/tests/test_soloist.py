"""Spotify via Soloist (SPEC v0.9 §8.1) against a fake Soloist WebSocket server.

The fake follows Soloist's WebSocket reference (message shapes checked against the real
1.3.8 binary for auth_state and errors): contexts of tracks, a queue with sources, autoplay
after the last title, and actions "from the Spotify app".
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import random
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from websockets.asyncio.server import Server, ServerConnection, serve

from myboxi_agent.adapters import soloist as soloist_module
from myboxi_agent.adapters.routing import RoutingPlayer
from myboxi_agent.adapters.soloist import SoloistClient, SoloistPlayer, is_explicit
from myboxi_agent.core.clock import FakeClock
from myboxi_agent.core.controller import Controller
from myboxi_agent.core.model import PlanItem, Playable, ResumePoint
from myboxi_agent.testing import (
    FakeAnnouncer,
    FakeLibrary,
    FakeOutbox,
    FakePlayer,
    FakeResumeStore,
    FakeSystem,
)
from myboxi_protocol.state import DeviceConfig

ALBUM = "spotify:album:4aawyAB9vmqN3uQ7FjRGTy"
OTHER = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
UID = "04A2B3C4D5E680"


def track(n: int, *, explicit: bool = False, prefix: str = "t") -> dict[str, Any]:
    return {
        "uri": f"spotify:track:{prefix}{n:021d}",
        "entity_type": "track",
        "decorations": {"playback": {"duration_ms": 180_000,
                                     "content_ratings": ["explicit"] if explicit else []}},
    }  # fmt: skip


@dataclass
class FakeSoloist:
    logged_in: bool = True
    contexts: dict[str, list[dict[str, Any]]] = field(
        default_factory=dict[str, list[dict[str, Any]]]
    )
    autoplay: list[dict[str, Any]] = field(
        default_factory=lambda: [track(n, prefix="a") for n in range(3)]
    )
    commands: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    context: str | None = None
    index: int = 0
    in_autoplay: bool = False
    status: str = "idle"
    volume: int = 40
    clients: list[ServerConnection] = field(default_factory=list[ServerConnection])
    server: Server | None = None
    port: int = 0

    async def start(self) -> None:
        self.server = await serve(self._handler, "127.0.0.1", 0)
        self.port = next(iter(self.server.sockets)).getsockname()[1]

    async def close(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()

    async def _handler(self, ws: ServerConnection) -> None:
        self.clients.append(ws)
        await self._send({"type": "auth_state", "logged_in": self.logged_in, "is_active": False,
                          "device_name": "Myboxi 4711"})  # fmt: skip
        try:
            async for raw in ws:
                await self._command(json.loads(raw))
        finally:
            self.clients.remove(ws)

    async def _send(self, *events: dict[str, Any]) -> None:
        for event in events:
            for ws in list(self.clients):
                await ws.send(json.dumps(event))

    # --- queue -------------------------------------------------------------------------------

    def _tracks(self) -> list[dict[str, Any]]:
        return self.contexts.get(self.context or "", [])

    def current(self) -> dict[str, Any]:
        return self.autoplay[0] if self.in_autoplay else self._tracks()[self.index]

    def _queue(self) -> dict[str, Any]:
        upcoming = [] if self.in_autoplay else [
            {"uid": t["uri"], "source": "context", "item": t}
            for t in self._tracks()[self.index + 1 :]
        ]  # fmt: skip
        upcoming += [{"uid": t["uri"], "source": "autoplay", "item": t} for t in self.autoplay]
        return {"type": "queue_changed", "previous": [], "upcoming": upcoming}

    async def _advance(self) -> None:
        if not self.in_autoplay and self.index + 1 < len(self._tracks()):
            self.index += 1
        else:
            self.in_autoplay = True
        await self._send({"type": "track_changed", "item": self.current()}, self._queue())

    # --- commands ----------------------------------------------------------------------------

    async def _command(self, msg: dict[str, Any]) -> None:
        self.commands.append(msg)
        name = msg["command"]
        if name == "get_auth_state":
            await self._send({"type": "auth_state", "logged_in": self.logged_in,
                              "is_active": True, "device_name": "Myboxi 4711"})  # fmt: skip
            return
        if not self.logged_in:
            await self._send({"type": "error", "message": "command requires authentication"})
            return
        match name:
            case "play" if "uri" in msg:
                self.context, self.index, self.in_autoplay = msg["uri"], 0, False
                self.status = "playing"
                await self._send(
                    {"type": "context_changed", "context": {"uri": self.context}},
                    {"type": "track_changed", "item": self.current()},
                    self._queue(),
                    {"type": "playback_changed", "status": "playing"},
                )
            case "play":
                self.status = "playing"
                await self._send({"type": "playback_changed", "status": "playing"})
            case "pause":
                self.status = "paused"
                await self._send({"type": "playback_changed", "status": "paused"})
            case "skip_next":
                await self._advance()
            case "seek":
                position = {"position_ms": msg["position_ms"], "timestamp_ms": 0, "speed": 1.0}
                await self._send({"type": "position_sync", "position": position})
            case "set_volume":
                self.volume = msg["volume"]
                await self._send({"type": "volume_changed", "volume": self.volume})
            case _:
                pass
        await self._send({"type": "command_result", "command": name})

    # --- the Spotify app ---------------------------------------------------------------------

    async def track_ends(self) -> None:
        await self._advance()

    async def app_play(self, uri: str) -> None:
        self.context, self.index, self.in_autoplay, self.status = uri, 0, False, "playing"
        await self._send(
            {"type": "context_changed", "context": {"uri": uri}},
            {"type": "track_changed", "item": self.current()},
            self._queue(),
            {"type": "playback_changed", "status": "playing"},
        )

    async def app_status(self, status: str) -> None:
        self.status = status
        await self._send({"type": "playback_changed", "status": status})

    async def app_login(self) -> None:
        self.logged_in = True
        await self._send({"type": "auth_state", "logged_in": True, "is_active": True,
                          "device_name": "Myboxi 4711"})  # fmt: skip

    async def app_volume(self, volume: int) -> None:
        self.volume = volume
        await self._send({"type": "volume_changed", "volume": volume})

    def sent(self, name: str) -> list[dict[str, Any]]:
        return [c for c in self.commands if c["command"] == name]

    def names(self) -> list[str]:
        return [c["command"] for c in self.commands]


@dataclass
class Rig:
    soloist: FakeSoloist
    player: SoloistPlayer
    events: list[tuple[str, Any]]
    task: asyncio.Task[None]
    explicit_allowed: list[bool]


async def settle(rounds: int = 20) -> None:
    for _ in range(rounds):
        await asyncio.sleep(0.01)


@pytest.fixture
async def rig() -> AsyncIterator[Rig]:
    fake = FakeSoloist(contexts={
        ALBUM: [track(n) for n in range(6)],
        OTHER: [track(n, prefix="o") for n in range(4)],
    })  # fmt: skip
    await fake.start()
    events: list[tuple[str, Any]] = []
    allowed = [False]
    player = SoloistPlayer(
        lambda on_event, on_connection: SoloistClient(fake.port, on_event, on_connection),
        FakeClock(),
        allow_explicit=lambda: allowed[0],
    )
    player.on_playlist_finished = lambda: events.append(("finished", None))
    player.on_error = lambda code: events.append(("error", code))
    player.on_remote_playing = lambda playing: events.append(("remote_playing", playing))
    player.on_external_started = lambda: events.append(("external_started", player.position()))
    player.on_external_volume = lambda v: events.append(("external_volume", v))
    task = asyncio.create_task(player.client.run())
    await asyncio.wait_for(player.client.connected.wait(), 5)
    await settle()
    yield Rig(fake, player, events, task, allowed)
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task
    await fake.close()


async def test_connects_only_to_loopback() -> None:
    """SPEC §10: the WebSocket client never talks to another address."""
    client = SoloistClient(24879, lambda _e: None)
    assert client.url == "ws://127.0.0.1:24879"


async def test_login_state_from_auth_state(rig: Rig) -> None:
    assert rig.player.logged_in
    assert rig.player.device_name == "Myboxi 4711"


async def test_play_from_the_start(rig: Rig) -> None:
    rig.player.set_volume(35)
    rig.player.play_context(ALBUM, ResumePoint(0, 0), False, "off")
    await settle()
    assert rig.soloist.sent("play")[0] == {"type": "command", "command": "play", "uri": ALBUM}
    assert rig.soloist.sent("set_shuffle") == [{"type": "command", "command": "set_shuffle",
                                                "enabled": False}]  # fmt: skip
    assert [c["command"] for c in rig.soloist.commands if "repeat" in c["command"]] == [
        "set_repeat_track",
        "set_repeat_context",
    ]
    assert rig.player.position() == ResumePoint(0, 0, track(0)["uri"])
    assert rig.events == []


async def test_resume_skips_muted_then_seeks(rig: Rig) -> None:
    """SPEC v0.9 §8.1: muted play, pause, N × skip_next, URI check, seek, volume back, play."""
    rig.player.set_volume(35)
    await settle()
    rig.soloist.commands.clear()
    rig.player.play_context(ALBUM, ResumePoint(3, 42_000, track(3)["uri"]), False, "off")
    await settle(40)
    names = rig.soloist.names()
    assert names.index("set_volume") < names.index("play")
    assert rig.soloist.sent("set_volume")[0]["volume"] == 0  # muted while skipping
    assert names.count("skip_next") == 3
    assert rig.soloist.sent("seek") == [
        {"type": "command", "command": "seek", "position_ms": 42_000}
    ]
    assert rig.soloist.sent("set_volume")[-1]["volume"] == 35
    assert names.index("seek") < len(names) - 1 - names[::-1].index("play")  # play after seek
    assert rig.soloist.status == "playing"
    pos = rig.player.position()
    assert pos is not None
    assert (pos.item_index, pos.item_key) == (3, track(3)["uri"])
    assert rig.events == []  # nothing looked like an action from the app


async def test_resume_starts_over_when_the_track_moved(rig: Rig) -> None:
    rig.player.play_context(
        ALBUM, ResumePoint(2, 10_000, "spotify:track:somethingelse00000000"), False, "off"
    )
    await settle(40)
    assert rig.soloist.names().count("play") >= 2  # the context again from the start
    assert rig.soloist.sent("seek") == []
    pos = rig.player.position()
    assert pos is not None
    assert pos.item_index == 0


async def test_index_follows_the_tracks(rig: Rig) -> None:
    rig.player.play_context(ALBUM, ResumePoint(0, 0), False, "off")
    await settle()
    await rig.soloist.track_ends()
    await rig.soloist.track_ends()
    await settle()
    pos = rig.player.position()
    assert pos is not None
    assert (pos.item_index, pos.item_key) == (2, track(2)["uri"])


async def test_autoplay_after_the_last_title_ends_the_content(rig: Rig) -> None:
    """SPEC v0.9 §8.1: the guard against catalog drift."""
    rig.player.play_context(ALBUM, ResumePoint(5, 0, track(5)["uri"]), False, "off")
    await settle(40)
    rig.soloist.commands.clear()
    await rig.soloist.track_ends()  # Spotify continues with an autoplay title
    await settle()
    assert rig.soloist.names() == ["pause"]
    assert rig.events == [("finished", None)]
    assert rig.player.figure is None


async def test_explicit_titles_are_skipped(rig: Rig) -> None:
    rig.soloist.contexts[ALBUM][1] = track(1, explicit=True)
    rig.player.play_context(ALBUM, ResumePoint(0, 0), False, "off")
    await settle()
    rig.soloist.commands.clear()
    await rig.soloist.track_ends()  # the explicit title starts …
    await settle()
    assert rig.soloist.names() == ["skip_next"]  # … and is skipped at once
    pos = rig.player.position()
    assert pos is not None
    assert pos.item_index == 2  # the skipped title still counts in the context


async def test_explicit_titles_play_when_allowed(rig: Rig) -> None:
    rig.explicit_allowed[0] = True
    rig.soloist.contexts[ALBUM][1] = track(1, explicit=True)
    rig.player.play_context(ALBUM, ResumePoint(0, 0), False, "off")
    await settle()
    rig.soloist.commands.clear()
    await rig.soloist.track_ends()
    await settle()
    assert rig.soloist.names() == []


async def test_only_explicit_titles_end_with_an_error(rig: Rig) -> None:
    rig.soloist.contexts[ALBUM] = [track(0)] + [track(n, explicit=True) for n in range(1, 13)]
    rig.player.play_context(ALBUM, ResumePoint(0, 0), False, "off")
    await settle()
    await rig.soloist.track_ends()
    await settle(60)
    assert ("error", "explicit") in rig.events
    assert rig.soloist.status == "paused"


async def test_another_context_from_the_app_hands_over(rig: Rig) -> None:
    rig.player.play_context(ALBUM, ResumePoint(0, 0), False, "off")
    await settle()
    await rig.soloist.track_ends()
    await settle()
    await rig.soloist.app_play(OTHER)
    await settle()
    # the figure's position was still readable when the controller was told
    assert rig.events[0] == ("external_started", ResumePoint(1, 0, track(1)["uri"]))
    assert rig.player.figure is None
    assert "pause" not in rig.soloist.names()  # the app's choice keeps playing


async def test_pause_and_play_in_the_app(rig: Rig) -> None:
    rig.player.play_context(ALBUM, ResumePoint(0, 0), False, "off")
    await settle()
    await rig.soloist.app_status("paused")
    await rig.soloist.app_status("playing")
    await settle()
    assert rig.events == [("remote_playing", False), ("remote_playing", True)]


async def test_own_commands_are_not_app_actions(rig: Rig) -> None:
    rig.player.play_context(ALBUM, ResumePoint(0, 0), False, "off")
    await settle()
    rig.player.pause()
    rig.player.resume()
    rig.player.set_volume(30)
    await settle()
    assert rig.events == []


async def test_volume_from_the_app_goes_to_the_policy(rig: Rig) -> None:
    rig.player.set_volume(30)
    await settle()
    await rig.soloist.app_volume(90)
    await settle()
    assert rig.events == [("external_volume", 90)]


async def test_volume_is_set_after_the_first_login(rig: Rig) -> None:
    """Soloist starts at volume 0 and refuses commands until an account is connected."""
    rig.player.set_volume(35)
    await settle()
    rig.player.logged_in = False
    rig.soloist.commands.clear()
    await rig.soloist.app_login()
    await settle()
    assert rig.soloist.sent("set_volume") == [
        {"type": "command", "command": "set_volume", "volume": 35}
    ]


async def test_not_logged_in(rig: Rig) -> None:
    rig.player.logged_in = False
    rig.player.play_context(ALBUM, ResumePoint(0, 0), False, "off")
    assert rig.events == [("error", "not_logged_in")]


async def test_soloist_gone_is_reported() -> None:
    events: list[str] = []
    player = SoloistPlayer(
        lambda on_event, on_connection: SoloistClient(9, on_event, on_connection),
        FakeClock(),
        allow_explicit=lambda: False,
    )
    player.on_error = events.append
    player.play_context(ALBUM, ResumePoint(0, 0), False, "off")
    assert events == ["not_running"]


async def test_a_step_that_times_out_is_an_error(rig: Rig, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(soloist_module, "STEP_TIMEOUT_S", 0.2)
    rig.soloist.contexts[ALBUM] = []  # Soloist never reports a track
    rig.soloist.contexts.pop(ALBUM)

    async def silent(msg: dict[str, Any]) -> None:
        rig.soloist.commands.append(msg)

    rig.soloist._command = silent  # type: ignore[method-assign]
    rig.player.play_context(ALBUM, ResumePoint(0, 0), False, "off")
    await asyncio.sleep(0.5)
    assert rig.events == [("error", "soloist_error")]


def test_explicit_flag() -> None:
    assert is_explicit(track(1, explicit=True))
    assert not is_explicit(track(1))
    assert not is_explicit({"uri": "x"})


# --- controller + routing + Soloist --------------------------------------------------------


async def test_figure_on_spotify_with_the_volume_policy(rig: Rig) -> None:
    """SPEC §9.2: the app's volume slider cannot go above max_volume."""
    clock = FakeClock(dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.UTC))
    local = FakePlayer()
    local.on_playlist_finished = None  # type: ignore[attr-defined]
    local.on_error = None  # type: ignore[attr-defined]
    routing = RoutingPlayer(local, rig.player)  # type: ignore[arg-type]
    plan = Playable(uuid.uuid4(), uuid.uuid4(), (PlanItem(ALBUM, "Album", 0),), True, False,
                    "off", "spotify", context=True)  # fmt: skip
    ctl = Controller(
        clock=clock, player=routing, announcer=FakeAnnouncer(), outbox=FakeOutbox(),
        resume_store=FakeResumeStore(), library=FakeLibrary({UID: plan}), system=FakeSystem(),
        config=lambda: DeviceConfig(max_volume=55), rng=random.Random(1),
    )  # fmt: skip
    routing.on_external_volume = ctl.external_volume
    routing.on_remote_playing = ctl.remote_playing
    routing.on_playlist_finished = ctl.playlist_finished
    ctl.token_placed(UID)
    await settle()
    assert rig.soloist.status == "playing"
    await rig.soloist.app_volume(90)
    await settle()
    assert rig.soloist.volume == 55
    await rig.soloist.app_status("paused")
    await settle()
    assert ctl.status().status == "paused"
