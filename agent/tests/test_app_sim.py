"""Agent runtime with simulated hardware: control socket, loops, outbox (CLAUDE.md rule 2)."""

from __future__ import annotations

import asyncio
import os
import stat
import subprocess
import sys
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from myboxi_agent.adapters.bundle import sim_adapters
from myboxi_agent.adapters.sim import SimAnnouncer, SimPlayer, SimSystem
from myboxi_agent.app import App
from myboxi_agent.config import Settings
from myboxi_agent.control import request
from myboxi_agent.core.model import Prompt
from myboxi_protocol.events import event_adapter

UID = "04A2B3C4D5E680"


def make_settings(tmp_path: Path) -> Settings:
    return Settings(data_dir=tmp_path / "data", sim=True, log_format="console")


def add_local_library(app: App, tmp_path: Path) -> None:
    entries: list[tuple[str, int, str, int]] = []
    for i, sha in enumerate(("a" * 64, "b" * 64)):
        path = app.assets.path(sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        app.assets.register(sha, 1)
        entries.append((sha, 1, f"Teil {i + 1}", 1000))
    app.library.add_local(UID, "Bibi", "Folge 1", entries)


@pytest.fixture
async def running(tmp_path: Path) -> AsyncIterator[App]:
    app = App(make_settings(tmp_path), sim_adapters())
    add_local_library(app, tmp_path)
    task = asyncio.create_task(app.run())
    for _ in range(50):
        if app.settings.control_socket.exists():
            break
        await asyncio.sleep(0.02)
    yield app
    app.stop()
    await task


async def ctl(app: App, **payload: Any) -> dict[str, Any]:
    return await request(app.settings.control_socket, payload)


async def test_control_socket_is_private(running: App) -> None:
    mode = stat.S_IMODE(running.settings.control_socket.stat().st_mode)
    assert mode & 0o077 == 0


async def test_place_play_pause_remove(running: App) -> None:
    player = running.adapters.player
    announcer = running.announcer
    assert isinstance(player, SimPlayer)
    assert isinstance(announcer, SimAnnouncer)
    status = await ctl(running, cmd="place", uid=UID.lower())
    assert status["playback"] == "playing"
    assert announcer.history[0] == (Prompt.TONE_START,)
    assert player.volume == 35
    status = await ctl(running, cmd="press", button="play_pause")
    assert status["playback"] == "paused"
    await ctl(running, cmd="press", button="play_pause")
    status = await ctl(running, cmd="remove")
    assert status["playback"] == "paused"
    kinds = [e["type"] for e in running.outbox_repo.pending()]
    assert kinds[0] == "token_played"
    assert "resume_position" in kinds
    for envelope in running.outbox_repo.pending():
        event_adapter.validate_python(envelope)  # SPEC §6.5 with boot_id/mono_ms (§5.6)


async def test_unknown_figure_queues_event(running: App) -> None:
    await ctl(running, cmd="place", uid="04FFFFFFFF")
    pending = running.outbox_repo.pending()
    assert pending[-1]["type"] == "token_unknown"
    assert pending[-1]["data"] == {"uid": "04FFFFFFFF"}


async def test_volume_buttons_respect_max(running: App) -> None:
    status = await ctl(running, cmd="place", uid=UID)
    for _ in range(6):
        status = await ctl(running, cmd="press", button="volume_up")
    assert status["volume"] == 55  # SPEC §3.4 default max_volume


async def test_setup_combination(running: App, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("myboxi_agent.core.buttons.COMBO_HOLD_S", 0.3)
    await ctl(running, cmd="hold", buttons=["volume_up", "volume_down"], seconds=0.6)
    system = running.adapters.system
    assert isinstance(system, SimSystem)
    assert system.setup_requested == 1


async def test_shutdown_saves_position(tmp_path: Path) -> None:
    app = App(make_settings(tmp_path), sim_adapters())
    add_local_library(app, tmp_path)
    task = asyncio.create_task(app.run())
    await asyncio.sleep(0.2)
    await ctl(app, cmd="place", uid=UID)
    await asyncio.sleep(0.3)
    app.stop()
    await task
    session = app.controller.session
    assert session is not None
    saved = app.controller.resume_store.get(session.plan.token_id)
    assert saved is not None
    assert saved.position_ms > 0


def test_cli_run_and_sim_commands(tmp_path: Path) -> None:
    """``myboxi-agent run --sim`` in a subprocess, driven by ``myboxi-agent sim``."""
    data = tmp_path / "data"
    folder = tmp_path / "Folge 1"
    folder.mkdir()
    (folder / "01 Anfang.mp3").write_bytes(b"not really audio")
    env = os.environ | {"MYBOXI_AGENT_LOG_FORMAT": "console"}
    base = [sys.executable, "-m", "myboxi_agent.cli", "--data-dir", str(data)]
    subprocess.run([*base, "library", "add", UID, str(folder)], check=True, env=env)
    proc = subprocess.Popen([*base, "run", "--sim"], env=env)
    try:
        for _ in range(100):
            if (data / "control.sock").exists():
                break
            time.sleep(0.05)
        out = subprocess.run(
            [*base, "sim", "place", UID], check=True, env=env, capture_output=True, text=True
        ).stdout
        assert '"playback": "playing"' in out
        out = subprocess.run(
            [*base, "status"], check=True, env=env, capture_output=True, text=True
        ).stdout
        assert '"sim": true' in out
    finally:
        proc.terminate()
        assert proc.wait(timeout=10) == 0
