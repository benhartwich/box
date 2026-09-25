"""``myboxi-agent doctor``: self-check for the image build (offline) and on the box."""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import shutil
import subprocess
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import httpx

from myboxi_agent.adapters.mpv import KNOWN_PROMPTS
from myboxi_agent.config import Settings
from myboxi_agent.control import request

Level = Literal["ok", "warn", "fail"]
MARK = {"ok": "✓", "warn": "!", "fail": "✗"}


@dataclass(frozen=True)
class Check:
    name: str
    level: Level
    detail: str


def _run(cmd: list[str]) -> str:
    try:
        return subprocess.run(  # noqa: S603 - fixed argument lists
            cmd, capture_output=True, text=True, timeout=10, check=False
        ).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def check_prompts(settings: Settings) -> Check:
    dirs = [settings.custom_prompts_dir, settings.prompts_dir]
    missing = [p for p in KNOWN_PROMPTS if not any((d / f"{p}.opus").is_file() for d in dirs)]
    if missing:
        return Check("prompts", "fail", f"missing: {', '.join(missing[:5])}")
    return Check("prompts", "ok", f"{len(KNOWN_PROMPTS)} prompts in {settings.prompts_dir}")


def check_database(settings: Settings, offline: bool) -> Check:
    from myboxi_agent.adapters.clock import SystemClock
    from myboxi_agent.store.db import connect
    from myboxi_agent.store.repos import Database, StateRepo

    base = Path(tempfile.mkdtemp()) if offline else settings.data_dir
    try:
        db = Database(connect(base / "myboxi.db"), base / "assets", SystemClock())
        state = StateRepo(db).get()
    except Exception as exc:
        return Check("database", "fail", str(exc))
    return Check(
        "database", "ok", f"device {state.device_id}, paired={state.tenant_id is not None}"
    )


def check_binary(name: str, path: str) -> Check:
    found = shutil.which(path)
    return Check(name, "ok" if found else "fail", found or f"{path} not found")


def check_hw_libs() -> Check:
    missing: list[str] = []
    for module in ("gpiozero", "adafruit_pn532.i2c", "busio"):
        try:
            importlib.import_module(module)
        except Exception:
            missing.append(module)
    if missing:
        return Check("hardware libraries", "fail", f"not importable: {', '.join(missing)}")
    return Check("hardware libraries", "ok", "gpiozero, adafruit_pn532, blinka")


def check_nfc(settings: Settings) -> Check:
    if not Path("/dev/i2c-1").exists():
        return Check("NFC reader", "fail", "/dev/i2c-1 missing (dtparam=i2c_arm=on?)")
    try:
        from myboxi_agent.adapters.hardware import open_pn532

        device: object = open_pn532(settings.pn532_i2c_address)
        version = getattr(device, "firmware_version", None)
    except Exception as exc:
        return Check(
            "NFC reader",
            "fail",
            f"PN532 not answering at 0x{settings.pn532_i2c_address:02x}: {exc}",
        )
    return Check("NFC reader", "ok", f"PN532 firmware {version}")


def check_audio() -> Check:
    out = _run(["wpctl", "status"])
    if "Sinks:" not in out:
        return Check("audio", "fail", "PipeWire not running for this user")
    sinks = out.split("Sinks:", 1)[1].split("Sources:", 1)[0]
    names = [line.strip(" │*") for line in sinks.splitlines() if "." in line]
    if not names:
        return Check("audio", "fail", "no output device (MAX98357A overlay?)")
    return Check("audio", "ok", names[0][:60])


def check_agent(settings: Settings) -> tuple[Check, str | None]:
    try:
        status = asyncio.run(request(settings.control_socket, {"cmd": "status"}, limit_s=5))
    except (OSError, TimeoutError, ValueError):
        return Check("agent", "fail", "not running"), None
    detail = f"paired={status.get('paired')}, playback={status.get('playback')}"
    if status.get("last_error"):
        detail += f", last error: {status['last_error']}"
    return Check("agent", "ok", detail), status.get("server_url")


def check_server(url: str | None) -> Check:
    if not url:
        return Check("server", "warn", "no server URL configured")
    try:
        r = httpx.get(f"{url.rstrip('/')}/healthz", timeout=10)
    except httpx.HTTPError as exc:
        return Check("server", "fail", f"{url}: {exc}")
    return Check("server", "ok" if r.status_code == 200 else "fail", f"{url}: HTTP {r.status_code}")


def check_network() -> Check:
    state = _run(["nmcli", "-t", "-f", "STATE,CONNECTIVITY", "general"]).strip()
    if not state:
        return Check("network", "warn", "nmcli not available")
    return Check("network", "ok" if state.startswith("connected") else "fail", state)


def run_doctor(settings: Settings, *, offline: bool, echo: Callable[[str], None] = print) -> int:
    checks: list[Check] = [
        check_database(settings, offline),
        check_prompts(settings),
        check_binary("mpv", settings.mpv_path),
        check_binary("nmcli", "nmcli"),
    ]
    if offline:
        for module in ("gpiozero", "adafruit_pn532.i2c"):
            spec = importlib.util.find_spec(module.split(".")[0])
            checks.append(
                Check(
                    f"module {module}", "ok" if spec else "fail", "installed" if spec else "missing"
                )
            )
    else:
        checks += [check_hw_libs(), check_nfc(settings), check_audio(), check_network()]
        agent, url = check_agent(settings)
        checks += [agent, check_server(url or settings.default_server_url)]
    for c in checks:
        echo(f"{MARK[c.level]} {c.name}: {c.detail}")
    return 1 if any(c.level == "fail" for c in checks) else 0
