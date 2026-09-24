"""mpv over its JSON IPC (CLAUDE.md stack): one instance for content, one for prompts.

The Player/Announcer methods are commands: they queue IPC messages and return at once.
mpv reports back through events, which become controller callbacks.
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import logging
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from myboxi_agent.core.model import Prompt, ResumePoint
from myboxi_protocol.state import RepeatMode

log = logging.getLogger(__name__)

TONE_FALLBACK = "av://lavfi:sine=frequency=660:duration=0.25"
ERROR_FALLBACK = "av://lavfi:sine=frequency=330:duration=0.6"


def mpv_args(mpv: str, ipc: Path, audio_output: str, client_name: str) -> list[str]:
    return [
        mpv,
        "--idle=yes",
        "--no-video",
        "--no-terminal",
        "--really-quiet",
        f"--input-ipc-server={ipc}",
        f"--ao={audio_output}",
        f"--audio-client-name={client_name}",
        "--gapless-audio=weak",
        "--volume=0",
        "--volume-max=100",
    ]


class MpvClient:
    """Minimal JSON IPC client: ordered commands, events to a callback, auto-reconnect."""

    def __init__(self, ipc: Path, on_event: Callable[[dict[str, Any]], None]) -> None:
        self.ipc = ipc
        self.on_event = on_event
        self._queue: asyncio.Queue[list[Any]] = asyncio.Queue()
        self._ids = itertools.count(1)
        self.connected = asyncio.Event()

    def send(self, *command: Any) -> None:
        self._queue.put_nowait(list(command))

    async def run(self) -> None:
        while True:
            try:
                reader, writer = await asyncio.open_unix_connection(str(self.ipc))
            except (FileNotFoundError, ConnectionRefusedError):
                await asyncio.sleep(0.2)
                continue
            self.connected.set()
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._write(writer))
                    tg.create_task(self._read(reader))
            except* (ConnectionError, asyncio.IncompleteReadError, EOFError):
                log.warning("mpv IPC connection lost", extra={"ipc": str(self.ipc)})
            finally:
                self.connected.clear()
                writer.close()
            await asyncio.sleep(0.5)

    async def _write(self, writer: asyncio.StreamWriter) -> None:
        while True:
            command = await self._queue.get()
            payload = {"command": command, "request_id": next(self._ids)}
            writer.write(json.dumps(payload).encode() + b"\n")
            await writer.drain()

    async def _read(self, reader: asyncio.StreamReader) -> None:
        while True:
            line = await reader.readline()
            if not line:
                raise EOFError
            try:
                message: dict[str, Any] = json.loads(line)
            except ValueError:
                continue
            if "event" in message:
                self.on_event(message)
            elif message.get("error") not in (None, "success"):
                log.warning("mpv command failed", extra={"error": message.get("error")})


class MpvProcess:
    """Keeps an mpv process running; restarts it when it dies."""

    def __init__(self, args: Sequence[str], ipc: Path, on_restart: Callable[[], None]) -> None:
        self.args = list(args)
        self.ipc = ipc
        self.on_restart = on_restart

    async def run(self) -> None:
        first = True
        while True:
            with contextlib.suppress(FileNotFoundError):
                self.ipc.unlink()
            proc = await asyncio.create_subprocess_exec(
                *self.args, stdin=asyncio.subprocess.DEVNULL
            )
            if not first:
                self.on_restart()
            first = False
            try:
                code = await proc.wait()
            except asyncio.CancelledError:
                proc.terminate()
                with contextlib.suppress(ProcessLookupError):
                    await asyncio.wait_for(proc.wait(), 3)
                raise
            log.error("mpv exited", extra={"code": code})
            await asyncio.sleep(1)


class MpvPlayer:
    """``core.ports.Player`` on mpv."""

    def __init__(
        self, client_factory: Callable[[Callable[[dict[str, Any]], None]], MpvClient]
    ) -> None:
        self.client = client_factory(self._on_event)
        self.on_playlist_finished: Callable[[], None] | None = None
        self.on_error: Callable[[str], None] | None = None
        self._pos_index = 0
        self._pos_s = 0.0
        self._pending_start: float | None = None
        self._active = False  # expecting an end-of-playlist idle event
        self._observed = False

    def _observe(self) -> None:
        if not self._observed:
            self.client.send("observe_property", 1, "playlist-pos")
            self.client.send("observe_property", 2, "time-pos")
            self._observed = True

    def play(
        self, sources: Sequence[str], index: int, position_ms: int, repeat: RepeatMode
    ) -> None:
        self._observe()
        self._active = False
        self.client.send("stop")
        self.client.send("set_property", "loop-file", "inf" if repeat == "one" else "no")
        self.client.send("set_property", "loop-playlist", "inf" if repeat == "all" else "no")
        self._pending_start = position_ms / 1000 if position_ms > 0 else None
        if self._pending_start is not None:
            self.client.send("set_property", "start", f"{self._pending_start:.3f}")
        for source in sources:
            self.client.send("loadfile", source, "append")
        self.client.send("set_property", "pause", False)
        self.client.send("playlist-play-index", index)
        self._pos_index, self._pos_s = index, position_ms / 1000
        self._active = True

    def pause(self) -> None:
        self.client.send("set_property", "pause", True)

    def resume(self) -> None:
        self.client.send("set_property", "pause", False)

    def stop(self) -> None:
        self._active = False
        self.client.send("stop")

    def set_volume(self, volume: int) -> None:
        self.client.send("set_property", "volume", max(0, min(volume, 100)))

    def position(self) -> ResumePoint | None:
        return ResumePoint(self._pos_index, int(self._pos_s * 1000))

    def restarted(self) -> None:
        """mpv crashed and came back: the playlist is gone."""
        self._observed = False
        if self._active and self.on_error is not None:
            self._active = False
            self.on_error("player_restart")

    def _on_event(self, event: dict[str, Any]) -> None:
        name = event.get("event")
        if name == "property-change":
            data = event.get("data")
            if event.get("name") == "playlist-pos" and isinstance(data, int) and data >= 0:
                self._pos_index = data
            elif event.get("name") == "time-pos" and isinstance(data, int | float):
                self._pos_s = float(data)
        elif name == "file-loaded" and self._pending_start is not None:
            # "start" applies to every file loaded afterwards; only the first one resumes.
            self._pending_start = None
            self.client.send("set_property", "start", "none")
        elif name == "end-file" and event.get("reason") == "error":
            if self.on_error is not None:
                self.on_error("decode_error")
        elif name == "idle" and self._active:
            self._active = False
            if self.on_playlist_finished is not None:
                self.on_playlist_finished()


class MpvAnnouncer:
    """``core.ports.Announcer``: prompt files, own recordings first (SPEC v0.5 §4).

    A missing file becomes a generated tone, so a situation is never silent (SPEC §1.7).
    Volume follows the single volume policy through ``volume``.
    """

    def __init__(
        self,
        client_factory: Callable[[Callable[[dict[str, Any]], None]], MpvClient],
        prompt_dirs: Sequence[Path],
        volume: Callable[[], int],
    ) -> None:
        self.client = client_factory(lambda _event: None)
        self.prompt_dirs = list(prompt_dirs)
        self.volume = volume  # core.volume.prompt_volume via the controller (CLAUDE.md rule 4)

    def source(self, prompt: str) -> str:
        for directory in self.prompt_dirs:
            path = directory / f"{prompt}.opus"
            if path.is_file():
                return str(path)
        log.warning("prompt missing", extra={"prompt": prompt})
        return TONE_FALLBACK if prompt.startswith("tone_") else ERROR_FALLBACK

    def announce(self, *prompts: str) -> None:
        self.client.send("set_property", "volume", self.volume())
        for prompt in prompts:
            self.client.send("loadfile", self.source(str(prompt)), "append-play")


KNOWN_PROMPTS = [p.value for p in Prompt] + [f"digit_{d}" for d in "0123456789"]
