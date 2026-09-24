"""Real hardware (docs/hardware.md): PN532 over I2C, buttons via gpiozero, mpv, systemd.

The hardware libraries come from the optional extra ``hw`` and are imported lazily, so the
rest of the agent (and its tests) run anywhere.
"""

from __future__ import annotations

import asyncio
import importlib
import logging
import subprocess
from collections.abc import AsyncIterator, Callable
from typing import Any, Protocol

from myboxi_agent.adapters.base import ButtonEvent, Placed, ReaderEvent, Removed
from myboxi_agent.adapters.bundle import Adapters, Background, VolumeSource
from myboxi_agent.config import Settings

log = logging.getLogger(__name__)

REMOVED_AFTER_MISSES = 3  # ~0.6 s at 200 ms polls: debounce short read gaps


class Pn532(Protocol):
    def read_passive_target(self, timeout: float = ...) -> bytearray | None: ...


def open_pn532(address: int) -> Pn532:  # pragma: no cover - needs the hardware
    # Untyped hardware libraries from the "hw" extra (Adafruit Blinka + PN532).
    board: Any = importlib.import_module("board")
    busio: Any = importlib.import_module("busio")
    pn532_i2c: Any = importlib.import_module("adafruit_pn532.i2c")
    pn532: Any = pn532_i2c.PN532_I2C(busio.I2C(board.SCL, board.SDA), address=address, debug=False)
    pn532.SAM_configuration()
    return pn532


class Pn532Reader:
    """Polls for a passive target; placing and removing become events (SPEC §9.1)."""

    def __init__(self, opener: Callable[[], Pn532], poll_s: float) -> None:
        self.opener = opener
        self.poll_s = poll_s

    async def events(self) -> AsyncIterator[ReaderEvent]:
        current: str | None = None
        misses = 0
        device: Pn532 | None = None
        backoff = 1.0
        while True:
            if device is None:
                try:
                    device = await asyncio.to_thread(self.opener)
                    backoff = 1.0
                    log.info("NFC reader ready")
                except Exception:
                    log.exception("NFC reader not available")
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
            try:
                uid = await asyncio.to_thread(device.read_passive_target, 0.1)
            except Exception:
                log.exception("NFC read failed")
                device = None
                continue
            if uid:
                misses = 0
                text = bytes(uid).hex().upper()
                if text != current:
                    current = text
                    yield Placed(text)
            elif current is not None:
                misses += 1
                if misses >= REMOVED_AFTER_MISSES:
                    current, misses = None, 0
                    yield Removed()
            await asyncio.sleep(self.poll_s)


class GpioButtons:
    """gpiozero buttons to GND with internal pull-ups; callbacks run in gpiozero's thread."""

    def __init__(
        self, pins: dict[str, int], button_factory: Callable[[int], Any] | None = None
    ) -> None:
        self.pins = pins
        self.button_factory = button_factory
        self._buttons: list[Any] = []

    def _factory(self) -> Callable[[int], Any]:
        if self.button_factory is not None:
            return self.button_factory
        gpiozero: Any = importlib.import_module("gpiozero")
        return lambda pin: gpiozero.Button(pin, pull_up=True, bounce_time=0.03)

    async def events(self) -> AsyncIterator[ButtonEvent]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[ButtonEvent] = asyncio.Queue()
        factory = self._factory()
        for name, pin in self.pins.items():
            button = factory(pin)
            button.when_pressed = lambda n=name: loop.call_soon_threadsafe(
                queue.put_nowait, ButtonEvent(n, pressed=True)
            )
            button.when_released = lambda n=name: loop.call_soon_threadsafe(
                queue.put_nowait, ButtonEvent(n, pressed=False)
            )
            self._buttons.append(button)
        while True:
            yield await queue.get()


Runner = Callable[[list[str]], int]


def _run(cmd: list[str]) -> int:
    # Fixed argument lists only, no shell.
    return subprocess.run(cmd, check=False, timeout=30).returncode  # noqa: S603


class SystemdSystem:
    """``core.ports.System``: setup mode is the root service myboxi-setupd (polkit-allowed)."""

    def __init__(self, runner: Runner = _run) -> None:
        self.runner = runner
        self.on_repair: Callable[[], None] | None = None

    def request_setup_mode(self) -> None:
        code = self.runner(["systemctl", "start", "--no-block", "myboxi-setupd.service"])
        if code != 0:
            log.error("could not start setup mode", extra={"code": code})

    def request_repair(self) -> None:
        if self.on_repair is not None:
            self.on_repair()


def hardware_adapters(settings: Settings) -> Adapters:  # pragma: no cover - wiring for the Pi
    from myboxi_agent.adapters.clock import SystemClock
    from myboxi_agent.adapters.mpv import (
        MpvAnnouncer,
        MpvClient,
        MpvPlayer,
        MpvProcess,
        mpv_args,
    )

    run_dir = settings.data_dir / "run"
    run_dir.mkdir(parents=True, exist_ok=True)
    player_ipc, prompt_ipc = run_dir / "mpv-player.sock", run_dir / "mpv-prompts.sock"
    player = MpvPlayer(lambda on_event: MpvClient(player_ipc, on_event))
    background: list[Background] = [
        MpvProcess(
            mpv_args(settings.mpv_path, player_ipc, settings.audio_output, "myboxi"),
            player_ipc,
            player.restarted,
        ).run,
        player.client.run,
        MpvProcess(
            mpv_args(settings.mpv_path, prompt_ipc, settings.audio_output, "myboxi-prompts"),
            prompt_ipc,
            lambda: None,
        ).run,
    ]

    def announcer(volume: VolumeSource) -> MpvAnnouncer:
        a = MpvAnnouncer(
            lambda on_event: MpvClient(prompt_ipc, on_event),
            [settings.custom_prompts_dir, settings.prompts_dir],
            volume,
        )
        background.append(a.client.run)
        return a

    return Adapters(
        clock=SystemClock(),
        reader=Pn532Reader(lambda: open_pn532(settings.pn532_i2c_address), settings.reader_poll_s),
        buttons=GpioButtons({str(name): pin for name, pin in settings.pins.items()}),
        player=player,
        system=SystemdSystem(),
        announcer_factory=announcer,
        background=background,
    )
