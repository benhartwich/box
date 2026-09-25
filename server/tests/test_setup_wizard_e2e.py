"""The real agent (simulated hardware) through the web setup wizard (SPEC v0.6 §9.6).

A household is created in the web UI, the box is claimed with the code it announces, and the
wizard follows every step from what the box reports: self-test (with a broken NFC reader for
a while), button test, unknown figure adopted, content bound, loaded and played.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from myboxi_agent.adapters.bundle import sim_adapters
from myboxi_agent.adapters.sim import SimButtons, SimPlayer, SimReader
from myboxi_agent.app import App as AgentApp
from myboxi_agent.config import Settings as AgentSettings
from myboxi_agent.sync import engine as agent_engine
from myboxi_server.models import Asset, Content, ContentItem
from myboxi_server.models.enums import ContentKind
from myboxi_server.storage.base import relpath_for
from myboxi_server.storage.filesystem import FilesystemAssetStore

from .helpers import OPUS_MIME, login, make_tenant, sessionmaker_of

UID = "04C0FFEE123456"


async def eventually(check: Callable[[], Awaitable[bool]], seconds: float = 30) -> None:
    async with asyncio.timeout(seconds):
        while not await check():
            await asyncio.sleep(0.2)


async def stored_content(app: FastAPI, tid: uuid.UUID) -> uuid.UUID:
    """A collection with one stored asset, as after an upload and transcoding."""
    store: FilesystemAssetStore = app.state.asset_store
    data = os.urandom(2048)
    sha = hashlib.sha256(data).hexdigest()
    rel = relpath_for(sha, "opus")
    store.path(rel).parent.mkdir(parents=True, exist_ok=True)
    store.path(rel).write_bytes(data)
    async with sessionmaker_of(app)() as db:
        content = Content(tenant_id=tid, kind=ContentKind.COLLECTION, title="Folge 1", source={})
        asset = Asset(
            tenant_id=tid, sha256=sha, mime=OPUS_MIME, bytes=len(data), storage_path=rel,
            duration_ms=1000,
        )  # fmt: skip
        db.add_all([content, asset])
        await db.flush()
        db.add(
            ContentItem(
                tenant_id=tid, content_id=content.id, position=0, asset_id=asset.id,
                title="Teil 1", duration_ms=1000,
            )
        )  # fmt: skip
        await db.commit()
        return content.id


async def test_wizard_follows_a_real_box(
    live_server: str,
    app: FastAPI,
    client: httpx.AsyncClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(agent_engine, "POLL_S", 2.1)  # server allows one poll per 2 s window
    monkeypatch.setattr(agent_engine, "FAST_POLL_S", 1.0)
    monkeypatch.setattr(agent_engine, "SETUP_REPORT_S", 0.5)

    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    r = await client.post("/households", data={"name": "Familie Test", "csrf_token": csrf})
    tid = uuid.UUID(r.headers["location"].split("/")[2])

    agent = AgentApp(
        AgentSettings(data_dir=tmp_path / "box", sim=True, default_server_url=live_server),
        sim_adapters(),
    )
    reader, buttons = agent.adapters.reader, agent.adapters.buttons
    player = _local(agent.adapters.player)
    assert isinstance(reader, SimReader)
    assert isinstance(buttons, SimButtons)
    assert isinstance(player, SimPlayer)
    task = asyncio.create_task(agent.run())
    try:
        async with asyncio.timeout(30):
            while agent.controller.pairing_code is None:
                await asyncio.sleep(0.05)
        r = await client.post(
            f"/t/{tid}/boxes/add",
            data={"code": agent.controller.pairing_code, "name": "Kinderzimmer",
                  "csrf_token": csrf},
        )  # fmt: skip
        assert r.status_code == 303
        wizard = r.headers["location"]

        async def steps() -> dict[str, str]:
            html = (await client.get(wizard)).text
            found = re.findall(r'class="step step-(\w+)">.*?<strong>(.*?)</strong>', html, re.S)
            return {title: status for status, title in found}

        async def is_(title: str, status: str) -> bool:
            return (await steps()).get(title) == status

        await eventually(lambda: is_("Box meldet sich", "done"))
        assert await is_("Box holt ihre Zugangsdaten ab", "done")
        assert await is_("Selbsttest", "done")
        assert agent.sync.in_setup_phase()

        reader.fail()
        await eventually(lambda: is_("Selbsttest", "problem"))
        assert "NFC-Leser antwortet nicht" in (await client.get(wizard)).text
        reader.recover()
        await eventually(lambda: is_("Selbsttest", "done"))

        for button in ("play_pause", "volume_up", "volume_down", "next"):
            await buttons.hold([button], 0.05)
        await eventually(lambda: is_("Tastentest", "done"))

        reader.place(UID)  # unknown: the box says so and reports it
        await eventually(lambda: _has(client, wizard, f'name="uid" value="{UID}"'))
        r = await client.post(
            f"/t/{tid}/figures/adopt",
            data={"uid": UID, "label": "Bibi", "next": wizard, "csrf_token": csrf},
        )
        assert r.headers["location"] == wizard
        reader.remove()

        content_id = await stored_content(app, tid)
        page = (await client.get(wizard)).text
        token_id = re.search(r"/figures/([0-9a-f-]{36})/binding", page)
        assert token_id
        r = await client.post(
            f"/t/{tid}/figures/{token_id.group(1)}/binding",
            data={"content_id": str(content_id), "resume": "true", "next": wizard,
                  "csrf_token": csrf},
        )  # fmt: skip
        assert r.headers["location"] == wizard

        await eventually(lambda: is_("Box lädt den Inhalt", "done"))
        reader.place(UID)
        await eventually(lambda: _playing(player))
        await eventually(lambda: is_("Abspielen", "done"))
        page = (await client.get(wizard)).text
        assert "Fertig!" in page
        assert "8 von 8 Schritten erledigt" in page
    finally:
        agent.stop()
        await task


async def _has(client: httpx.AsyncClient, url: str, text: str) -> bool:
    return text in (await client.get(url)).text


async def _playing(player: SimPlayer) -> bool:
    return player.state == "playing"


def _local(player: object) -> object:
    """The file player behind the routing player (SPEC v0.9: mpv and Spotify)."""
    from myboxi_agent.adapters.routing import RoutingPlayer

    return player.local if isinstance(player, RoutingPlayer) else player
