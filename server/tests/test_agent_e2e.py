"""The real agent (simulated hardware) against the real server: SPEC §5.2, §7, §9.5."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from myboxi_agent.adapters.bundle import sim_adapters
from myboxi_agent.adapters.sim import SimAnnouncer, SimPlayer, SimReader
from myboxi_agent.app import App as AgentApp
from myboxi_agent.config import Settings as AgentSettings
from myboxi_agent.core.model import Action, Playable
from myboxi_agent.sync import engine as agent_engine
from myboxi_server.models import Device, DeviceConfig, Event

from .helpers import claim_code, make_tenant, seed_library, sessionmaker_of


async def wait_for(condition: Callable[[], object], seconds: float = 30) -> None:
    async with asyncio.timeout(seconds):
        while not condition():
            await asyncio.sleep(0.05)


async def test_agent_pairs_syncs_plays_and_reports(
    live_server: str, app: FastAPI, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent_engine, "POLL_S", 2.1)  # server allows one poll per 2 s window
    t = await make_tenant(app)
    lib = await seed_library(app, t.tenant_id, n_items=2)
    settings = AgentSettings(data_dir=tmp_path / "box", sim=True, default_server_url=live_server)
    agent = AgentApp(settings, sim_adapters())
    reader, player, announcer = (
        agent.adapters.reader,
        _local(agent.adapters.player),
        agent.announcer,
    )
    assert isinstance(reader, SimReader)
    assert isinstance(player, SimPlayer)
    assert isinstance(announcer, SimAnnouncer)
    task = asyncio.create_task(agent.run())
    try:
        # §9.5: the unpaired box announces its code; a user claims it in the app.
        await wait_for(lambda: agent.controller.pairing_code)
        code = agent.controller.pairing_code
        assert code is not None
        assert announcer.history[0] == ("hello",)
        assert announcer.history[1][2:] == tuple(f"digit_{d}" for d in code)
        await claim_code(app, t.tenant_id, code)

        # §5.2: pairing, token, snapshot, assets (SHA-256 checked), atomic activation.
        await wait_for(lambda: isinstance(agent.library.resolve(lib.token_uid), Playable))
        assert agent.state.get().tenant_id == t.tenant_id
        for sha in lib.shas:
            assert agent.assets.has(sha)

        # The figure plays; events and reported reach the server.
        reader.place(lib.token_uid)
        await wait_for(lambda: player.state == "playing")
        agent.sync.trigger()
        await wait_for(lambda: agent.outbox_repo.count() == 0)
        async with sessionmaker_of(app)() as db:
            types = set((await db.scalars(select(Event.type))).all())
            assert "token_played" in types
            device = await db.get(Device, agent.state.get().device_id)
            assert device is not None
            assert device.reported is not None
            assert device.reported["playback"]["status"] == "playing"

        # A new limit set in the app applies on the next sync (SPEC §3.4, §9.2).
        async with sessionmaker_of(app)() as db:
            await db.execute(
                update(DeviceConfig)
                .where(DeviceConfig.device_id == agent.state.get().device_id)
                .values(max_volume=20)
            )
            await db.commit()
        agent.sync.trigger()
        await wait_for(lambda: agent.state.device_config().max_volume == 20)
        agent.controller.tick()
        assert player.volume <= 20

        # §9.4: play_pause + next held → unpair and a new code.
        device_id = agent.state.get().device_id
        agent.controller.button(Action.REPAIR)
        await wait_for(lambda: agent.controller.pairing_code not in (None, code))
        async with sessionmaker_of(app)() as db:
            device = await db.get(Device, device_id)
            assert device is not None
            assert device.tenant_id is None
    finally:
        agent.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _local(player: object) -> object:
    """The file player behind the routing player (SPEC v0.9: mpv and Spotify)."""
    from myboxi_agent.adapters.routing import RoutingPlayer

    return player.local if isinstance(player, RoutingPlayer) else player
