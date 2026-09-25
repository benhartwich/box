"""Self-test of the running agent (SPEC v0.6 §6.4 ``health``)."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Any, cast

import pytest

from myboxi_agent import doctor
from myboxi_agent.adapters.audio import AudioMonitor, parse_sinks
from myboxi_agent.adapters.base import ButtonEvent, Placed
from myboxi_agent.adapters.bundle import sim_adapters
from myboxi_agent.adapters.hardware import GpioButtons, Pn532Reader
from myboxi_agent.adapters.health import Health
from myboxi_agent.adapters.sim import SimReader
from myboxi_agent.adapters.system_info import wifi_rssi
from myboxi_agent.app import App
from myboxi_agent.config import Settings
from myboxi_protocol.reported import ReportedData


class Pn532Stub:
    def __init__(self, fail_read: bool = False) -> None:
        self.fail_read = fail_read

    def read_passive_target(self, timeout: float = 0.1) -> bytearray | None:
        del timeout
        if self.fail_read:
            self.fail_read = False
            raise OSError("i2c read")
        return bytearray(bytes.fromhex("04112233"))


async def _first_event(reader: Pn532Reader) -> object:
    async for event in reader.events():
        return event
    raise AssertionError


@pytest.fixture(autouse=True)
def fast_sleep(monkeypatch: pytest.MonkeyPatch) -> None:
    real_sleep = asyncio.sleep

    async def sleep(delay: float, result: Any = None) -> Any:
        return await real_sleep(min(delay, 0.01), result)

    monkeypatch.setattr(asyncio, "sleep", sleep)


async def test_nfc_missing_bus_then_ok(tmp_path: Path) -> None:
    health = Health()
    seen: list[tuple[str, str] | None] = []
    stub = Pn532Stub()

    def opener() -> Pn532Stub:
        r = health.get("nfc")
        seen.append((r.level, r.code) if r else None)
        if len(seen) == 1:
            raise OSError("no bus")
        return stub

    reader = Pn532Reader(opener, 0.0, health=health, bus=tmp_path / "i2c-1")
    assert await asyncio.wait_for(_first_event(reader), 5) == Placed("04112233")
    assert seen == [None, ("fail", "no_i2c")]
    assert health.snapshot() == [{"check": "nfc", "level": "ok", "code": "ok"}]


async def test_nfc_not_responding_and_read_error(tmp_path: Path) -> None:
    bus = tmp_path / "i2c-1"
    bus.touch()
    health = Health()
    codes: list[str] = []
    attempts = 0

    def opener() -> Pn532Stub:
        nonlocal attempts
        attempts += 1
        r = health.get("nfc")
        codes.append(r.code if r else "-")
        if attempts == 1:
            raise OSError("no ack")
        return Pn532Stub(fail_read=attempts == 2)

    reader = Pn532Reader(opener, 0.0, health=health, bus=bus)
    assert await asyncio.wait_for(_first_event(reader), 5) == Placed("04112233")
    # first open fails, second opens but the read fails, third reopens and reads
    assert codes == ["-", "not_responding", "read_error"]
    assert health.snapshot() == [{"check": "nfc", "level": "ok", "code": "ok"}]


async def test_buttons_retry_instead_of_crashing() -> None:
    health = Health()
    calls = 0

    class Button:
        def __init__(self, pin: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("GPIO busy")
            self.when_pressed: Any = None
            self.when_released: Any = None
            Button.last = self

        last: Button

    buttons = GpioButtons({"next": 23}, button_factory=Button, health=health, retry_s=0.01)
    stream = cast(AsyncGenerator[ButtonEvent], buttons.events())
    task = asyncio.ensure_future(anext(stream))
    await asyncio.sleep(0)
    for _ in range(50):
        if calls >= 2:
            break
        await asyncio.sleep(0.01)
    assert health.snapshot() == [{"check": "buttons", "level": "ok", "code": "ok"}]
    Button.last.when_pressed()
    assert await asyncio.wait_for(task, 5) == ButtonEvent("next", pressed=True)
    await stream.aclose()


class Client:
    def __init__(self, up: bool) -> None:
        self.connected = asyncio.Event()
        if up:
            self.connected.set()


async def test_audio_monitor_codes() -> None:
    health = Health()
    await AudioMonitor(health, [Client(True), Client(False)], sinks=lambda: ["x"]).check()
    assert health.snapshot() == [{"check": "audio", "level": "fail", "code": "player_down"}]
    await AudioMonitor(health, [Client(True)], sinks=lambda: []).check()
    assert health.snapshot()[0]["code"] == "no_output"
    await AudioMonitor(health, [Client(True)], sinks=lambda: None).check()
    assert health.snapshot()[0]["code"] == "no_output"
    await AudioMonitor(health, [Client(True)], sinks=lambda: ["MAX98357A"]).check()
    assert health.snapshot()[0]["level"] == "ok"


def test_parse_wpctl_sinks() -> None:
    status = """Audio
 ├─ Devices:
 │      42. Built-in Audio
 ├─ Sinks:
 │  *   47. MAX98357A Digital Stereo [vol: 0.40]
 ├─ Sources:
"""
    assert parse_sinks(status) == ["47. MAX98357A Digital Stereo [vol: 0.40]"]
    assert parse_sinks(" ├─ Sinks:\n ├─ Sources:\n") == []
    assert parse_sinks("") is None


@pytest.mark.parametrize(
    ("line", "expected"),
    [
        ("wlan0: 0000   54.  -56.  -256        0      0      0      0      0        0", -56),
        ("wlan0: 0000   54.  200.  -256        0      0      0      0      0        0", -56),
        ("wlan0: 0000   0.   0.    0           0      0      0      0      0        0", 0),
    ],
)
def test_wifi_rssi(tmp_path: Path, line: str, expected: int) -> None:
    f = tmp_path / "wireless"
    f.write_text("Inter-| sta-|   Quality        |\n face | tus | link level noise |\n" + line)
    assert wifi_rssi(f) == expected


def test_wifi_rssi_without_wireless(tmp_path: Path) -> None:
    assert wifi_rssi(tmp_path / "missing") is None
    f = tmp_path / "wireless"
    f.write_text("header\nheader\n")
    assert wifi_rssi(f) is None


def test_reported_contains_health(tmp_path: Path) -> None:
    adapters = sim_adapters()
    app = App(Settings(data_dir=tmp_path, sim=True), adapters)
    data = ReportedData.model_validate(app.reported_data().model_dump(mode="json"))
    assert data.health is not None
    assert {h.check: h.level for h in data.health} == {
        "audio": "ok",
        "buttons": "ok",
        "nfc": "ok",
        "prompts": "ok",
    }
    assert isinstance(adapters.reader, SimReader)
    adapters.reader.fail()
    health = app.reported_data().health
    assert health is not None
    assert [(h.level, h.code) for h in health if h.check == "nfc"] == [("fail", "not_responding")]
    assert app.status()["health"] == adapters.health.snapshot()


def test_doctor_uses_the_running_agents_self_test(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    status: dict[str, Any] = {
        "server_url": None,
        "health": [
            {"check": "nfc", "level": "fail", "code": "not_responding"},
            {"check": "audio", "level": "ok", "code": "ok"},
        ],
    }

    def running_agent(_settings: Settings) -> tuple[doctor.Check, dict[str, Any]]:
        return doctor.Check("agent", "ok", ""), status

    monkeypatch.setattr(doctor, "check_agent", running_agent)
    monkeypatch.setattr(doctor, "check_hw_libs", lambda: doctor.Check("hw", "ok", ""))
    monkeypatch.setattr(doctor, "check_network", lambda: doctor.Check("network", "ok", ""))

    def must_not_open(_settings: Settings) -> doctor.Check:
        raise AssertionError("a second PN532 client disturbs the running agent")

    monkeypatch.setattr(doctor, "check_nfc", must_not_open)
    lines: list[str] = []
    settings = Settings(data_dir=tmp_path, prompts_dir=tmp_path, mpv_path="true")
    assert doctor.run_doctor(settings, offline=False, echo=lines.append) == 1
    assert "✗ NFC reader: PN532 not answering (SDA/SCL, 3.3 V, DIP switch set to I2C?)" in lines
    assert "✓ audio: ok (running agent)" in lines
