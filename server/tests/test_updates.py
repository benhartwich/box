"""Box software updates in the web UI (SPEC v0.7 §3.4, §6.4, §11.1)."""

from __future__ import annotations

import asyncio
import datetime as dt
import json
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from myboxi_protocol.reported import ReportedData
from myboxi_protocol.state import StateResponse
from myboxi_server.domain.updates import Release, UpdateChannel, software_view

from .helpers import login, make_tenant, pair_device

NOW = dt.datetime(2026, 9, 26, 12, tzinfo=dt.UTC)
FRESH = Release("0.3.1", NOW - dt.timedelta(hours=2))
OLD = Release("0.3.1", NOW - dt.timedelta(days=3))


def data(version: str = "0.3.0", update: dict[str, Any] | None = None) -> ReportedData:
    return ReportedData.model_validate(
        {
            "agent_version": version, "hw_model": "rpi4", "applied_config_rev": 0,
            "applied_device_rev": 0, "storage": {"free_mb": 1000}, "time_trusted": True,
            "playback": {"status": "stopped", "volume": 30}, "update": update,
        }
    )  # fmt: skip


@pytest.mark.parametrize(
    ("version", "update", "latest", "level", "words", "notice"),
    [
        ("0.3.1", {"state": "installed", "version": "0.3.1"}, FRESH, "ok", "Aktuell", False),
        ("0.3.0", {"state": "up_to_date"}, FRESH, "info", "0.3.1 ist erschienen", False),
        ("0.3.0", {"state": "up_to_date"}, OLD, "info", "0.3.1 ist erschienen", True),
        ("0.3.0", {"state": "waiting", "version": "0.3.1"}, FRESH, "info", "sobald nichts", False),
        ("0.3.0", {"state": "available", "version": "0.3.1"}, FRESH, "info", "Automatische", False),
        ("0.3.0", {"state": "failed", "code": "no_space"}, FRESH, "fail", "zu wenig Platz", True),
        ("0.3.0", {"state": "rolled_back", "version": "0.3.1", "code": "unhealthy"}, FRESH,
         "fail", "zurückgekehrt", True),
        ("0.2.0", None, OLD, "warn", "neue Image", True),
        ("0.3.1", None, FRESH, "ok", "Aktuell", False),
        ("0.3.0", {"state": "up_to_date"}, None, "ok", "Aktuell", False),
    ],
)  # fmt: skip
def test_software_in_words(
    version: str,
    update: dict[str, Any] | None,
    latest: Release | None,
    level: str,
    words: str,
    notice: bool,
) -> None:
    view = software_view(data(version, update), latest, NOW)
    assert (view.level, view.notice) == (level, notice)
    assert words in view.text


async def test_channel_refreshes_in_the_background() -> None:
    manifest = {
        "channel": "stable", "version": "0.3.1", "released_at": "2026-09-25T12:00:00Z",
        "bundle": {"url": "https://example.org/b.tar.xz", "sha256": "a" * 64, "size": 10},
    }  # fmt: skip
    calls: list[str] = []

    def fetch(url: str) -> bytes:
        calls.append(url)
        return json.dumps(manifest).encode()

    channel = UpdateChannel("https://example.org/manifest.json", fetch)
    assert channel.latest() is None  # never waits for the network
    for _ in range(50):
        if channel.latest() is not None:
            break
        await asyncio.sleep(0.01)
    latest = channel.latest()
    assert latest is not None
    assert latest.version == "0.3.1"
    assert len(calls) == 1  # cached for an hour


async def test_unreachable_or_broken_manifest_is_unknown() -> None:
    def offline(url: str) -> bytes:
        raise OSError("offline")

    channel = UpdateChannel("https://example.org/manifest.json", offline)
    await channel.refresh()
    assert channel.latest() is None
    broken = UpdateChannel("https://example.org/manifest.json", lambda _url: b"{}")
    await broken.refresh()
    assert broken._latest is None  # pyright: ignore[reportPrivateUsage]
    assert UpdateChannel(None).latest() is None


async def test_box_page_and_home_show_the_update(
    app: FastAPI, client: httpx.AsyncClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    await login(client, t.owner_email)
    body = {
        "v": 1, "id": "01J8Z3M5W6XK2C4B7N9V000001", "ts": "2026-09-25T10:00:00Z",
        "type": "reported",
        "data": data("0.3.0", {"state": "rolled_back", "version": "0.3.1", "code": "unhealthy"})
        .model_dump(mode="json"),
    }  # fmt: skip
    r = await client.post("/api/v1/device/reported", headers=dev.auth, json=body)
    assert r.status_code == 204
    channel = UpdateChannel(None)
    channel._latest = FRESH  # pyright: ignore[reportPrivateUsage]
    monkeypatch.setattr(app.state, "update_channel", channel)
    page = (await client.get(f"/t/{t.tenant_id}/boxes/{dev.device_id}")).text
    assert "Software" in page
    assert "Version 0.3.0" in page
    assert "zurückgekehrt" in page
    home = (await client.get(f"/t/{t.tenant_id}/")).text
    assert "Software von" in home


async def test_auto_update_setting_reaches_the_box(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    csrf = await login(client, t.owner_email)

    async def state() -> StateResponse:
        r = await client.get("/api/v1/device/state", headers=dev.auth)
        return StateResponse.model_validate_json(r.content)

    assert (await state()).device_config.auto_update is True
    form = {
        "max_volume": "55", "start_volume": "35", "on_token_removed": "pause",
        "locale": "de-AT", "timezone": "Europe/Vienna", "csrf_token": csrf,
    }  # fmt: skip
    r = await client.post(f"/t/{t.tenant_id}/boxes/{dev.device_id}/config", data=form)
    assert r.status_code == 200
    assert (await state()).device_config.auto_update is False  # unchecked box
    r = await client.post(
        f"/t/{t.tenant_id}/boxes/{dev.device_id}/config", data=form | {"auto_update": "true"}
    )
    assert (await state()).device_config.auto_update is True
    page = (await client.get(f"/t/{t.tenant_id}/boxes/{dev.device_id}")).text
    assert 'name="auto_update" value="true" checked' in page
