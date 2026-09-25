"""Hardware adapters with fakes; the real-mpv test runs where mpv is installed (CI)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import math
import shutil
import struct
import wave
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from myboxi_agent.adapters.base import ButtonEvent, Placed, Removed
from myboxi_agent.adapters.clock import SystemClock
from myboxi_agent.adapters.hardware import GpioButtons, Pn532Reader, SystemdSystem
from myboxi_agent.adapters.mpv import (
    ERROR_FALLBACK,
    TONE_FALLBACK,
    MpvAnnouncer,
    MpvClient,
    MpvPlayer,
    MpvProcess,
    mpv_args,
)
from myboxi_agent.core.model import PlanItem, ResumePoint

# --- a fake mpv IPC endpoint ------------------------------------------------------------------


class FakeMpv:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.commands: list[list[Any]] = []
        self.writer: asyncio.StreamWriter | None = None
        self.server: asyncio.Server | None = None

    async def start(self) -> None:
        self.server = await asyncio.start_unix_server(self._client, path=str(self.path))

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.writer = writer
        while line := await reader.readline():
            msg = json.loads(line)
            self.commands.append(msg["command"])
            writer.write(
                json.dumps({"request_id": msg["request_id"], "error": "success"}).encode() + b"\n"
            )
            await writer.drain()

    async def emit(self, **event: Any) -> None:
        assert self.writer is not None
        self.writer.write(json.dumps(event).encode() + b"\n")
        await self.writer.drain()
        await asyncio.sleep(0.05)

    async def settle(self) -> None:
        await asyncio.sleep(0.1)


@pytest.fixture
async def fake_mpv(tmp_path: Path) -> AsyncIterator[FakeMpv]:
    mpv = FakeMpv(tmp_path / "mpv.sock")
    await mpv.start()
    yield mpv
    assert mpv.server is not None
    mpv.server.close()


async def test_player_commands_and_events(fake_mpv: FakeMpv) -> None:
    player = MpvPlayer(lambda on_event: MpvClient(fake_mpv.path, on_event))
    finished: list[bool] = []
    errors: list[str] = []
    player.on_playlist_finished = lambda: finished.append(True)
    player.on_error = errors.append
    task = asyncio.create_task(player.client.run())
    try:
        player.play(
            [PlanItem("/a/0.opus", "A", 0), PlanItem("/a/1.opus", "B", 0)], 1, 12_500, "all"
        )
        player.set_volume(35)
        await fake_mpv.settle()
        assert fake_mpv.commands[:2] == [
            ["observe_property", 1, "playlist-pos"],
            ["observe_property", 2, "time-pos"],
        ]
        assert ["set_property", "loop-playlist", "inf"] in fake_mpv.commands
        assert ["set_property", "loop-file", "no"] in fake_mpv.commands
        assert ["set_property", "start", "12.500"] in fake_mpv.commands
        assert fake_mpv.commands.count(["loadfile", "/a/0.opus", "append"]) == 1
        assert fake_mpv.commands[-2:] == [
            ["playlist-play-index", 1],
            ["set_property", "volume", 35],
        ]
        await fake_mpv.emit(event="file-loaded")
        assert fake_mpv.commands[-1] == ["set_property", "start", "none"]
        await fake_mpv.emit(event="property-change", id=1, name="playlist-pos", data=1)
        await fake_mpv.emit(event="property-change", id=2, name="time-pos", data=13.25)
        assert player.position() == ResumePoint(1, 13_250)
        await fake_mpv.emit(event="end-file", reason="error")
        assert errors == ["decode_error"]
        await fake_mpv.emit(event="idle")
        assert finished == [True]
        player.stop()
        await fake_mpv.emit(event="idle")
        assert finished == [True]  # an intentional stop is not the end of the content
    finally:
        task.cancel()


async def test_player_applies_loudness_gain_per_file(fake_mpv: FakeMpv) -> None:
    """SPEC v0.8 §8.2: the correction belongs to one file; a boost goes through a limiter."""
    player = MpvPlayer(lambda on_event: MpvClient(fake_mpv.path, on_event))
    task = asyncio.create_task(player.client.run())
    try:
        items = [
            PlanItem("/p/loud.mp3", "laut", 0, key="a" * 32, gain_db=-4.24),
            PlanItem("/p/quiet.mp3", "leise", 0, key="b" * 32, gain_db=6.0),
            PlanItem("/p/plain.mp3", "normal", 0, key="c" * 32, gain_db=0.04),
        ]
        player.play(items, 0, 0, "off")
        await fake_mpv.settle()
        loads = [c for c in fake_mpv.commands if "loadfile" in json.dumps(c)]
        assert loads == [
            {"name": "loadfile", "url": "/p/loud.mp3", "flags": "append",
             "options": "af=%21%lavfi=[volume=-4.2dB]"},
            {"name": "loadfile", "url": "/p/quiet.mp3", "flags": "append",
             "options": "af=%49%lavfi=[volume=6.0dB,alimiter=limit=0.891:level=0]"},
            ["loadfile", "/p/plain.mp3", "append"],
        ]  # fmt: skip
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def test_announcer_prefers_own_recordings_and_never_stays_silent(
    fake_mpv: FakeMpv, tmp_path: Path
) -> None:
    own, generated = tmp_path / "own", tmp_path / "generated"
    own.mkdir()
    generated.mkdir()
    (generated / "hello.opus").write_bytes(b"x")
    (own / "hello.opus").write_bytes(b"x")
    (generated / "loading.opus").write_bytes(b"x")
    announcer = MpvAnnouncer(lambda e: MpvClient(fake_mpv.path, e), [own, generated], lambda: 42)
    task = asyncio.create_task(announcer.client.run())
    try:
        announcer.announce("hello", "loading", "tone_start", "digit_7")
        await fake_mpv.settle()
        assert fake_mpv.commands == [
            ["set_property", "volume", 42],
            ["loadfile", str(own / "hello.opus"), "append-play"],
            ["loadfile", str(generated / "loading.opus"), "append-play"],
            ["loadfile", TONE_FALLBACK, "append-play"],
            ["loadfile", ERROR_FALLBACK, "append-play"],
        ]
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _sine_wav(path: Path, seconds: float) -> None:
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        frames = b"".join(
            struct.pack("<h", int(8000 * math.sin(2 * math.pi * 440 * i / 16000)))
            for i in range(int(16000 * seconds))
        )
        w.writeframes(frames)


@pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")
async def test_real_mpv_plays_resumes_and_finishes(tmp_path: Path) -> None:
    for i in range(2):
        _sine_wav(tmp_path / f"{i}.wav", 1.0)
    ipc = tmp_path / "player.sock"
    player = MpvPlayer(lambda e: MpvClient(ipc, e))
    finished = asyncio.Event()
    player.on_playlist_finished = finished.set
    proc = MpvProcess(mpv_args("mpv", ipc, "null", "test"), ipc, player.restarted)
    tasks = [asyncio.create_task(proc.run()), asyncio.create_task(player.client.run())]
    try:
        await asyncio.wait_for(player.client.connected.wait(), 10)
        player.set_volume(10)
        player.play(
            [
                PlanItem(str(tmp_path / f"{i}.wav"), f"T{i}", 1000, gain_db=g)
                for i, g in ((0, 3.0), (1, None))
            ],
            0,
            500,
            "off",
        )
        await asyncio.sleep(0.8)
        pos = player.position()
        assert pos is not None
        assert pos.item_index in (0, 1)
        await asyncio.wait_for(finished.wait(), 10)
    finally:
        for t in tasks:
            t.cancel()
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t


# --- PN532 reader -------------------------------------------------------------------------


class FakePn532:
    def __init__(self, reads: list[bytes | None]) -> None:
        self.reads = reads

    def read_passive_target(self, timeout: float = 0.1) -> bytearray | None:
        del timeout
        value = self.reads.pop(0) if self.reads else None
        return bytearray(value) if value else None


async def _collect(reader: Pn532Reader, count: int) -> list[object]:
    out: list[object] = []
    async for ev in reader.events():
        out.append(ev)
        if len(out) == count:
            break
    return out


async def test_reader_debounces_removal() -> None:
    uid = bytes.fromhex("04A2B3C4D5E680")
    device = FakePn532([uid, uid, None, uid, None, None, None, None])
    events = await asyncio.wait_for(_collect(Pn532Reader(lambda: device, 0.0), 2), 5)
    assert events == [Placed("04A2B3C4D5E680"), Removed()]  # one missed read is not a removal


async def test_reader_reopens_after_errors() -> None:
    attempts: list[int] = []

    def opener() -> FakePn532:
        attempts.append(1)
        if len(attempts) == 1:
            raise OSError("no I2C device")
        return FakePn532([bytes.fromhex("04112233")])

    reader = Pn532Reader(opener, 0.0)
    events = await asyncio.wait_for(_collect(reader, 1), 5)
    assert events == [Placed("04112233")]
    assert len(attempts) == 2


# --- buttons, system, clock ---------------------------------------------------------------


class FakeGpioButton:
    instances: ClassVar[dict[int, FakeGpioButton]] = {}

    def __init__(self, pin: int) -> None:
        self.pin = pin
        self.when_pressed: Any = None
        self.when_released: Any = None
        FakeGpioButton.instances[pin] = self


async def test_gpio_buttons_deliver_events_from_callbacks() -> None:
    buttons = GpioButtons({"play_pause": 17, "next": 23}, button_factory=FakeGpioButton)
    stream = buttons.events()
    first = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0.05)
    FakeGpioButton.instances[17].when_pressed()  # gpiozero calls these from its thread
    assert await asyncio.wait_for(first, 1) == ButtonEvent("play_pause", pressed=True)
    FakeGpioButton.instances[23].when_released()
    assert await asyncio.wait_for(anext(stream), 1) == ButtonEvent("next", pressed=False)


def test_setup_mode_starts_the_setup_service() -> None:
    calls: list[list[str]] = []
    system = SystemdSystem(runner=lambda cmd: calls.append(cmd) or 0)
    system.request_setup_mode()
    assert calls == [["systemctl", "start", "--no-block", "myboxi-setupd.service"]]


def test_clock_trust_follows_ntp_and_sticks() -> None:
    answers = ["NTPSynchronized=no\n", "NTPSynchronized=yes\n", "NTPSynchronized=no\n"]
    clock = SystemClock(runner=lambda _cmd: answers.pop(0))
    assert not clock.time_trusted()
    clock._checked = -1e9  # pyright: ignore[reportPrivateUsage]
    assert clock.time_trusted()
    clock._checked = -1e9  # pyright: ignore[reportPrivateUsage]
    assert clock.time_trusted()  # stays trusted until reboot (SPEC §5.6)
