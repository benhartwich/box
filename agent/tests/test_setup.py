"""Setup mode (SPEC v0.5 §9.3): nmcli adapter, captive portal, setup service, auto start."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator, Sequence
from dataclasses import dataclass, field

import httpx
import pytest

from myboxi_agent.core.clock import FakeClock
from myboxi_agent.core.model import Prompt
from myboxi_agent.setup import daemon as daemon_module
from myboxi_agent.setup.daemon import run_setup, spoken_name
from myboxi_agent.setup.nm import (
    HOTSPOT,
    Network,
    NetworkManager,
    Result,
    network_name,
    split_terse,
)
from myboxi_agent.setup.portal import Portal, Submission
from myboxi_agent.setup.watch import OFFLINE_GRACE_S, RETRIGGER_AFTER_S, NetworkWatch

SECRET = "sehr-geheimes-passwort"


# --- nmcli ------------------------------------------------------------------------------------


@dataclass
class FakeRunner:
    answers: dict[str, Result] = field(default_factory=dict[str, Result])
    calls: list[list[str]] = field(default_factory=list[list[str]])

    def __call__(self, args: Sequence[str], timeout: float = 30) -> Result:
        del timeout
        self.calls.append(list(args))
        key = " ".join(args)
        for prefix, result in self.answers.items():
            if key.startswith(prefix):
                return result
        return Result(0, "")


def test_terse_output_with_escaped_colons() -> None:
    assert split_terse(r"Mein\:WLAN:72:WPA2") == ["Mein:WLAN", "72", "WPA2"]
    assert split_terse(r"a\\b:1") == ["a\\b", "1"]


def test_scan_keeps_the_strongest_entry_per_ssid() -> None:
    out = "Heim:40:WPA2\nHeim:80:WPA2\nOffen:55:--\n:90:WPA2\n"
    nm = NetworkManager(FakeRunner({"-t -f SSID": Result(0, out)}))
    assert nm.scan() == [Network("Heim", 80, True), Network("Offen", 55, False)]


def test_wifi_configured_ignores_the_setup_hotspot() -> None:
    runner = FakeRunner({"-t -f NAME,TYPE": Result(0, f"{HOTSPOT}:802-11-wireless\nlo:loopback\n")})
    assert not NetworkManager(runner).wifi_configured()
    runner.answers["-t -f NAME,TYPE"] = Result(0, "Heim:802-11-wireless\n")
    assert NetworkManager(runner).wifi_configured()


def test_hotspot_is_an_open_shared_access_point() -> None:
    runner = FakeRunner()
    assert NetworkManager(runner).start_hotspot("Myboxi-1234")
    add = runner.calls[1]
    assert add[:4] == ["connection", "add", "type", "wifi"]
    assert add[add.index("ssid") : add.index("ssid") + 2] == ["ssid", "Myboxi-1234"]
    assert "ap" in add
    assert "shared" in add
    assert "10.42.0.1/24" in add
    assert runner.calls[2] == ["connection", "up", HOTSPOT]


def test_connect_wifi_success_and_cleanup_on_failure(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.DEBUG)
    runner = FakeRunner()
    assert NetworkManager(runner).connect_wifi("Heim", SECRET)
    assert runner.calls[1][-2:] == ["wifi-sec.psk", SECRET]
    runner = FakeRunner({"--wait": Result(4, "failed")})
    assert not NetworkManager(runner).connect_wifi("Heim", SECRET)
    assert runner.calls[-1] == ["connection", "delete", "myboxi-wifi-Heim"]
    assert SECRET not in caplog.text


def test_network_name_has_four_readable_digits() -> None:
    name = network_name("100000003f2a1b7c")
    assert name.startswith("Myboxi-")
    assert name[-4:].isdigit()
    assert spoken_name(name) == [f"digit_{d}" for d in name[-4:]]
    assert network_name(None)[-4:].isdigit()


# --- portal -----------------------------------------------------------------------------------


@pytest.fixture
async def portal() -> AsyncIterator[tuple[Portal, str]]:
    p = Portal(
        [Network("Heim", 80, True), Network('<script>alert("x")</script>', 50, False)],
        "https://app.myboxi.eu",
    )
    server = await p.serve("127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    yield p, f"http://127.0.0.1:{port}"
    server.close()


async def test_portal_page_lists_networks_escaped(portal: tuple[Portal, str]) -> None:
    _, url = portal
    async with httpx.AsyncClient() as c:
        r = await c.get(url + "/")
    assert r.status_code == 200
    assert "Heim 🔒" in r.text
    assert "<script>" not in r.text
    assert "&lt;script&gt;" in r.text
    assert 'value="https://app.myboxi.eu"' in r.text


@pytest.mark.parametrize("probe", ["/generate_204", "/connecttest.txt", "/anything", "/ncsi.txt"])
async def test_captive_probes_redirect(portal: tuple[Portal, str], probe: str) -> None:
    _, url = portal
    async with httpx.AsyncClient() as c:
        r = await c.get(url + probe)
    assert r.status_code == 302
    assert r.headers["location"] == "http://10.42.0.1/"


async def test_apple_probe_gets_the_page(portal: tuple[Portal, str]) -> None:
    _, url = portal
    async with httpx.AsyncClient() as c:
        r = await c.get(url + "/hotspot-detect.html")
    assert r.status_code == 200
    assert "Myboxi einrichten" in r.text


async def test_valid_submission(portal: tuple[Portal, str]) -> None:
    p, url = portal
    form = {"ssid": "Heim", "password": SECRET, "server_url": "https://app.myboxi.eu/"}
    async with httpx.AsyncClient() as c:
        r = await c.post(url + "/connect", data=form)
    assert "Verbinde mit Heim" in r.text
    assert p.submission is not None
    assert p.submission.result() == Submission("Heim", SECRET, "https://app.myboxi.eu")


@pytest.mark.parametrize(
    "form",
    [
        {"ssid": "Heim", "password": "kurz", "server_url": "https://app.myboxi.eu"},
        {"ssid": "", "password": SECRET, "server_url": "https://app.myboxi.eu"},
        {"ssid": "Heim", "password": SECRET, "server_url": "javascript:alert(1)"},
    ],
)
async def test_invalid_submission_shows_an_error(
    portal: tuple[Portal, str], form: dict[str, str]
) -> None:
    p, url = portal
    async with httpx.AsyncClient() as c:
        r = await c.post(url + "/connect", data=form)
    assert 'class="err"' in r.text
    assert p.submission is not None
    assert not p.submission.done()


async def test_manual_ssid_wins_and_open_network_is_allowed(portal: tuple[Portal, str]) -> None:
    p, url = portal
    form = {
        "ssid": "Heim",
        "ssid_manual": "Gast",
        "password": "",
        "server_url": "http://nas.local:8000",
    }
    async with httpx.AsyncClient() as c:
        await c.post(url + "/connect", data=form)
    assert p.submission is not None
    assert p.submission.result() == Submission("Gast", "", "http://nas.local:8000")


async def test_oversized_request_is_rejected(portal: tuple[Portal, str]) -> None:
    _, url = portal
    async with httpx.AsyncClient() as c:
        r = await c.post(
            url + "/connect", content=b"x" * 10_000, headers={"content-type": "text/plain"}
        )
    assert r.status_code == 400


# --- setup service ----------------------------------------------------------------------------


@dataclass
class FakeNM:
    connect_results: list[bool] = field(default_factory=list[bool])
    hotspots: int = 0
    stopped: int = 0
    connected: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def scan(self) -> list[Network]:
        return [Network("Heim", 70, True)]

    def start_hotspot(self, ssid: str) -> bool:
        del ssid
        self.hotspots += 1
        return True

    def stop_hotspot(self) -> None:
        self.stopped += 1

    def connect_wifi(self, ssid: str, password: str) -> bool:
        self.connected.append((ssid, password))
        return self.connect_results.pop(0)


@dataclass
class FakeAgent:
    said: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])
    urls: list[str] = field(default_factory=list[str])

    async def announce(self, *prompts: str) -> None:
        self.said.append(tuple(str(p) for p in prompts))

    async def set_server_url(self, url: str) -> None:
        self.urls.append(url)


async def _submit_when_ready(form: dict[str, str], attempts: int = 1) -> None:
    """Act as the phone: post the form to each new portal (the service picks a free port)."""
    for used in range(attempts):
        async with asyncio.timeout(5):
            while len(_PORTS) <= used:
                await asyncio.sleep(0.01)
        async with httpx.AsyncClient() as c:
            await c.post(f"http://127.0.0.1:{_PORTS[used]}/connect", data=form)


_PORTS: list[int] = []


@pytest.fixture(autouse=True)
def capture_port(monkeypatch: pytest.MonkeyPatch) -> None:
    real = Portal.serve

    async def serve(self: Portal, host: str, port: int) -> asyncio.Server:
        server = await real(self, host, 0)
        _PORTS.append(server.sockets[0].getsockname()[1])
        return server

    _PORTS.clear()
    monkeypatch.setattr(Portal, "serve", serve)
    monkeypatch.setattr(daemon_module.asyncio, "sleep", _fast_sleep)


_real_sleep = asyncio.sleep


async def _fast_sleep(seconds: float) -> None:
    await _real_sleep(min(seconds, 0.01))


async def test_setup_connects_and_hands_over_the_server_url() -> None:
    nm, agent = FakeNM(connect_results=[True]), FakeAgent()
    form = {"ssid": "Heim", "password": SECRET, "server_url": "https://app.myboxi.eu"}
    phone = asyncio.create_task(_submit_when_ready(form))
    ok = await run_setup(
        nm, agent, ssid="Myboxi-0042", default_server_url="https://app.myboxi.eu", host="127.0.0.1"
    )
    await phone
    assert ok
    assert agent.said[0] == (Prompt.SETUP_START, "digit_0", "digit_0", "digit_4", "digit_2")
    assert agent.said[-1] == (Prompt.SETUP_CONNECTED,)
    assert agent.urls == ["https://app.myboxi.eu"]
    assert nm.connected == [("Heim", SECRET)]
    assert nm.stopped == 1


async def test_failed_connection_reopens_the_portal() -> None:
    nm, agent = FakeNM(connect_results=[False, True]), FakeAgent()
    form = {"ssid": "Heim", "password": SECRET, "server_url": "https://app.myboxi.eu"}
    phone = asyncio.create_task(_submit_when_ready(form, attempts=2))
    ok = await run_setup(
        nm, agent, ssid="Myboxi-0042", default_server_url="https://x.test", host="127.0.0.1"
    )
    await phone
    assert ok
    assert (Prompt.SETUP_FAILED,) in agent.said
    assert nm.hotspots == 2


async def test_setup_ends_after_inactivity() -> None:
    nm, agent = FakeNM(), FakeAgent()
    ok = await run_setup(
        nm,
        agent,
        ssid="Myboxi-0042",
        default_server_url="https://x.test",
        host="127.0.0.1",
        inactivity_s=0.2,
    )
    assert not ok
    assert agent.said[-1] == (Prompt.SETUP_END,)
    assert nm.stopped == 1


# --- automatic start --------------------------------------------------------------------------


@dataclass
class StateNM:
    online_: bool = False
    ethernet: bool = False
    wifi: bool = False

    def online(self) -> bool:
        return self.online_

    def ethernet_connected(self) -> bool:
        return self.ethernet

    def wifi_configured(self) -> bool:
        return self.wifi


@dataclass
class CountingSystem:
    setups: int = 0

    def request_setup_mode(self) -> None:
        self.setups += 1

    def request_repair(self) -> None:
        pass


def _watch(
    nm: StateNM, clock: FakeClock, system: CountingSystem, online: list[int]
) -> NetworkWatch:
    return NetworkWatch(nm, system, clock, lambda: online.append(1), is_setup_active=lambda: False)


def test_no_wifi_starts_setup_at_once_and_not_too_often() -> None:
    nm, clock, system = StateNM(), FakeClock(), CountingSystem()
    online: list[int] = []
    w = _watch(nm, clock, system, online)
    w.check()
    assert system.setups == 1
    clock.advance(60)
    w.check()
    assert system.setups == 1
    clock.advance(RETRIGGER_AFTER_S)
    w.check()
    assert system.setups == 2


def test_lost_wifi_starts_setup_after_two_minutes_and_reconnect_syncs() -> None:
    nm, clock, system = StateNM(wifi=True), FakeClock(), CountingSystem()
    online: list[int] = []
    w = _watch(nm, clock, system, online)
    w.check()
    clock.advance(OFFLINE_GRACE_S - 1)
    w.check()
    assert system.setups == 0
    clock.advance(1)
    w.check()
    assert system.setups == 1
    nm.online_ = True
    w.check()
    assert online == [1]  # SPEC §5.2: sync right after reconnecting


def test_ethernet_never_starts_setup() -> None:
    nm, clock, system = StateNM(ethernet=True), FakeClock(), CountingSystem()
    online: list[int] = []
    w = _watch(nm, clock, system, online)
    clock.advance(OFFLINE_GRACE_S * 3)
    w.check()
    assert system.setups == 0
