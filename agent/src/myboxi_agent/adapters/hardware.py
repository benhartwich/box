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
from pathlib import Path
from typing import Any, Protocol

from myboxi_agent.adapters.base import ButtonEvent, Placed, ReaderEvent, Removed
from myboxi_agent.adapters.bundle import Adapters, Background, VolumeSource
from myboxi_agent.adapters.health import Health
from myboxi_agent.config import Settings

log = logging.getLogger(__name__)

REMOVED_AFTER_MISSES = 3  # ~0.6 s at 200 ms polls: debounce short read gaps
I2C_BUS = Path("/dev/i2c-1")


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
    """Polls for a passive target; placing and removing become events (SPEC §9.1).

    Self-test ``nfc`` (SPEC v0.6 §6.4): ``no_i2c``, ``not_responding``, ``read_error``.
    """

    def __init__(
        self,
        opener: Callable[[], Pn532],
        poll_s: float,
        health: Health | None = None,
        bus: Path | None = None,
    ) -> None:
        self.opener = opener
        self.poll_s = poll_s
        self.health = health or Health()
        self.bus = bus

    def _open_failed(self) -> None:
        missing_bus = self.bus is not None and not self.bus.exists()
        self.health.set("nfc", "fail", "no_i2c" if missing_bus else "not_responding")

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
                    self.health.ok("nfc")
                except Exception:
                    log.exception("NFC reader not available")
                    self._open_failed()
                    await asyncio.sleep(backoff)
                    backoff = min(backoff * 2, 30)
                    continue
            try:
                uid = await asyncio.to_thread(device.read_passive_target, 0.1)
            except Exception:
                log.exception("NFC read failed")
                self.health.set("nfc", "warn", "read_error")
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
    """gpiozero buttons to GND with internal pull-ups; callbacks run in gpiozero's thread.

    Self-test ``buttons``: ``gpio_error`` when the pins cannot be set up; the agent keeps
    running (figures still play) and retries.
    """

    def __init__(
        self,
        pins: dict[str, int],
        button_factory: Callable[[int], Any] | None = None,
        health: Health | None = None,
        retry_s: float = 1.0,
    ) -> None:
        self.pins = pins
        self.button_factory = button_factory
        self.health = health or Health()
        self.retry_s = retry_s
        self._buttons: list[Any] = []

    def _factory(self) -> Callable[[int], Any]:
        if self.button_factory is not None:
            return self.button_factory
        gpiozero: Any = importlib.import_module("gpiozero")
        return lambda pin: gpiozero.Button(pin, pull_up=True, bounce_time=0.03)

    def _open(self, loop: asyncio.AbstractEventLoop, queue: asyncio.Queue[ButtonEvent]) -> None:
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

    def _close(self) -> None:
        for button in self._buttons:
            close = getattr(button, "close", None)
            if callable(close):
                close()
        self._buttons.clear()

    async def events(self) -> AsyncIterator[ButtonEvent]:
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue[ButtonEvent] = asyncio.Queue()
        backoff = self.retry_s
        while True:
            try:
                self._open(loop, queue)
                break
            except Exception:
                log.exception("buttons not available")
                self._close()
                self.health.set("buttons", "fail", "gpio_error")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 60)
        self.health.ok("buttons")
        while True:
            yield await queue.get()


Runner = Callable[[list[str]], int]


def run_command(cmd: list[str]) -> int:
    # Fixed argument lists only, no shell.
    return subprocess.run(cmd, check=False, timeout=60).returncode  # noqa: S603


class SystemdSystem:
    """``core.ports.System``: setup mode is the root service myboxi-setupd (polkit-allowed)."""

    def __init__(self, runner: Runner = run_command) -> None:
        self.runner = runner
        self.on_repair: Callable[[], None] | None = None

    def request_setup_mode(self) -> None:
        code = self.runner(["systemctl", "start", "--no-block", "myboxi-setupd.service"])
        if code != 0:
            log.error("could not start setup mode", extra={"code": code})

    def request_repair(self) -> None:
        if self.on_repair is not None:
            self.on_repair()

    def request_update(self) -> None:
        """SPEC v0.7 §11.1: look for an update now (online again); polkit allows exactly this."""
        code = self.runner(["systemctl", "start", "--no-block", "myboxi-updater.service"])
        if code != 0:
            log.warning("could not start the updater", extra={"code": code})


def hardware_adapters(settings: Settings) -> Adapters:  # pragma: no cover - wiring for the Pi
    from myboxi_agent.adapters.audio import AudioMonitor
    from myboxi_agent.adapters.clock import SystemClock
    from myboxi_agent.adapters.mpv import (
        MpvAnnouncer,
        MpvClient,
        MpvPlayer,
        MpvProcess,
        missing_prompts,
        mpv_args,
    )

    health = Health()
    prompt_dirs = [settings.custom_prompts_dir, settings.prompts_dir]
    if missing_prompts(prompt_dirs):
        health.set("prompts", "warn", "missing")
    else:
        health.ok("prompts")

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
        a = MpvAnnouncer(lambda on_event: MpvClient(prompt_ipc, on_event), prompt_dirs, volume)
        background.append(a.client.run)
        background.append(AudioMonitor(health, [player.client, a.client]).run)
        return a

    return Adapters(
        clock=SystemClock(),
        reader=Pn532Reader(
            lambda: open_pn532(settings.pn532_i2c_address),
            settings.reader_poll_s,
            health=health,
            bus=I2C_BUS,
        ),
        buttons=GpioButtons({str(name): pin for name, pin in settings.pins.items()}, health=health),
        player=player,
        system=SystemdSystem(),
        announcer_factory=announcer,
        background=background,
        health=health,
    )
