"""Setup wizard: every step follows what the box reports (SPEC v0.6 §6.4, §9.6)."""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import os
import re
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from myboxi_server.domain.revisions import tenant_config_rev
from myboxi_server.models import Asset, Content, ContentItem, Device, Token
from myboxi_server.models.enums import ContentKind, Role
from myboxi_server.storage.base import relpath_for

from .helpers import (
    OPUS_MIME,
    PairedDevice,
    add_member,
    login,
    make_tenant,
    pair_device,
    sessionmaker_of,
    start_pairing,
)

BOOT = "0192d7c4-5b1e-7c3a-9f00-000000000001"
_ids = itertools.count(1)
ALL_OK = [{"check": c, "level": "ok", "code": "ok"} for c in ("audio", "buttons", "nfc", "prompts")]


def _ulid() -> str:
    return f"01J8Z3M5W6XK2C4B7N9Q{next(_ids):06d}"


@dataclass
class Box:
    client: httpx.AsyncClient
    dev: PairedDevice

    async def report(self, **data: Any) -> None:
        body = {
            "agent_version": "0.2.0",
            "image_version": "0.2.0",
            "hw_model": "rpi4",
            "applied_config_rev": 0,
            "applied_device_rev": 0,
            "storage": {"free_mb": 20000},
            "wifi_rssi": -55,
            "time_trusted": True,
            "playback": {"status": "stopped", "volume": 30},
        } | data
        r = await self.client.post(
            "/api/v1/device/reported",
            headers=self.dev.auth,
            json={"v": 1, "id": _ulid(), "ts": "2026-09-25T10:00:00Z", "type": "reported",
                  "data": body},
        )  # fmt: skip
        assert r.status_code == 204, r.text

    async def event(self, type_: str, data: dict[str, Any]) -> None:
        envelope = {
            "v": 1, "id": _ulid(), "ts": "2026-09-25T10:00:00Z", "type": type_,
            "boot_id": BOOT, "mono_ms": 1000, "data": data,
        }  # fmt: skip
        r = await self.client.post(
            "/api/v1/device/events", headers=self.dev.auth, json={"events": [envelope]}
        )
        assert r.status_code == 200, r.text


@dataclass
class Wizard:
    app: FastAPI
    client: httpx.AsyncClient
    tid: uuid.UUID
    csrf: str
    box: Box

    @property
    def url(self) -> str:
        return f"/t/{self.tid}/boxes/{self.box.dev.device_id}/setup"

    async def steps(self) -> dict[str, str]:
        """Step title -> status as rendered."""
        html = (await self.client.get(self.url)).text
        found = re.findall(r'class="step step-(\w+)">.*?<strong>(.*?)</strong>', html, re.S)
        return {title: status for status, title in found}

    async def status_of(self, title: str) -> str:
        return (await self.steps())[title]

    async def post(self, path: str, **data: str) -> httpx.Response:
        return await self.client.post(f"/t/{self.tid}{path}", data=data | {"csrf_token": self.csrf})


@pytest.fixture
async def wizard(app: FastAPI, client: httpx.AsyncClient) -> Wizard:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    csrf = await login(client, t.owner_email)
    return Wizard(app, client, t.tenant_id, csrf, Box(client, dev))


async def _content(app: FastAPI, tid: uuid.UUID, kind: ContentKind, items: int) -> uuid.UUID:
    async with sessionmaker_of(app)() as db:
        content = Content(tenant_id=tid, kind=kind, title="Folge 1", source={})
        db.add(content)
        await db.flush()
        for i in range(items):
            data = os.urandom(512)
            sha = hashlib.sha256(data).hexdigest()
            asset = Asset(
                tenant_id=tid, sha256=sha, mime=OPUS_MIME, bytes=len(data),
                storage_path=relpath_for(sha, "opus"), duration_ms=1000,
            )  # fmt: skip
            db.add(asset)
            await db.flush()
            db.add(
                ContentItem(
                    tenant_id=tid, content_id=content.id, position=i, asset_id=asset.id,
                    title=f"Teil {i + 1}", duration_ms=1000,
                )
            )  # fmt: skip
        await db.commit()
        return content.id


async def test_full_setup_is_followed_step_by_step(wizard: Wizard) -> None:
    w, box = wizard, wizard.box
    assert await w.status_of("Box holt ihre Zugangsdaten ab") == "done"  # pair_device polled
    assert await w.status_of("Box meldet sich") == "active"

    # Report with a broken NFC reader and two buttons pressed.
    health = [h if h["check"] != "nfc" else h | {"level": "fail", "code": "not_responding"}
              for h in ALL_OK]  # fmt: skip
    await box.report(health=health, button_test={"seen": ["play_pause", "next"]})
    page = (await w.client.get(w.url)).text
    assert "WLAN-Signal gut" in page
    assert "NFC-Leser antwortet nicht" in page
    assert await w.status_of("Selbsttest") == "problem"
    assert await w.status_of("Tastentest") == "active"
    assert page.count('class="chip chip-on"') == 2

    # Reader fixed, all buttons pressed.
    seen = ["play_pause", "volume_up", "volume_down", "next"]
    await box.report(health=ALL_OK, button_test={"seen": seen})
    assert await w.status_of("Selbsttest") == "done"
    assert await w.status_of("Tastentest") == "done"
    assert await w.status_of("Erste Figur") == "active"

    # An unknown figure shows up with the adopt form; adopting returns to the wizard.
    uid = "04A1B2C3D4E5F6"
    await box.event("token_unknown", {"uid": uid})
    page = (await w.client.get(w.url)).text
    assert f'name="uid" value="{uid}"' in page
    r = await w.post("/figures/adopt", uid=uid, label="Bibi", next=w.url)
    assert r.status_code == 303
    assert r.headers["location"] == w.url
    assert await w.status_of("Erste Figur") == "done"
    assert await w.status_of("Inhalt zuordnen") == "active"

    # Bind content; the box has not loaded it yet.
    content_id = await _content(w.app, w.tid, ContentKind.COLLECTION, items=2)
    async with sessionmaker_of(w.app)() as db:
        token_id = await db.scalar(select(Token.id).where(Token.uid == uid))
    r = await w.post(
        f"/figures/{token_id}/binding", content_id=str(content_id), resume="true", next=w.url
    )
    assert r.headers["location"] == w.url
    assert await w.status_of("Inhalt zuordnen") == "done"
    assert await w.status_of("Box lädt den Inhalt") == "active"

    async with sessionmaker_of(w.app)() as db:
        rev = await tenant_config_rev(db, w.tid)
    await box.report(health=ALL_OK, button_test={"seen": seen}, applied_config_rev=rev)
    assert await w.status_of("Box lädt den Inhalt") == "done"
    assert await w.status_of("Abspielen") == "active"

    await box.event("token_played", {"token_id": str(token_id), "content_id": str(content_id)})
    steps = await w.steps()
    assert len(steps) == 8
    assert set(steps.values()) == {"done"}
    page = (await w.client.get(w.url)).text
    assert "Fertig!" in page
    assert "hx-get" not in page  # polling stops


async def test_status_fragment_only_when_something_changed(wizard: Wizard) -> None:
    w = wizard
    r = await w.client.get(f"{w.url}/status")
    key = re.search(r'"v": "([0-9a-f]+)"', r.text)
    assert key
    assert (await w.client.get(f"{w.url}/status", params={"v": key.group(1)})).status_code == 204
    await w.box.report(health=ALL_OK)
    r = await w.client.get(f"{w.url}/status", params={"v": key.group(1)})
    assert r.status_code == 200
    assert 'id="setup-steps"' in r.text


async def test_old_image_skips_self_test_and_button_test(wizard: Wizard) -> None:
    await wizard.box.report()  # 0.1.0 agents send neither health nor button_test
    page = (await wizard.client.get(wizard.url)).text
    assert "ab Version 0.2" in page
    assert await wizard.status_of("Selbsttest") == "skipped"
    assert await wizard.status_of("Tastentest") == "skipped"


async def test_uncollected_credentials_are_a_problem(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    started = await start_pairing(client)
    r = await client.post(
        f"/t/{t.tenant_id}/boxes/add",
        data={"code": started.code, "name": "Kinderzimmer", "csrf_token": csrf},
    )
    url = r.headers["location"]
    device_id = uuid.UUID(url.split("/")[4])
    page = (await client.get(url)).text
    assert "Geschafft!" in page  # still waiting for the box
    async with sessionmaker_of(app)() as db:
        await db.execute(
            update(Device)
            .where(Device.id == device_id)
            .values(paired_at=dt.datetime.now(dt.UTC) - dt.timedelta(minutes=11))
        )
        await db.commit()
    page = (await client.get(url)).text
    assert "der Code ist verfallen" in page


async def test_streams_cannot_play_yet(wizard: Wizard) -> None:
    w = wizard
    await w.box.report(health=ALL_OK)
    content_id = await _content(w.app, w.tid, ContentKind.STREAM, items=0)
    async with sessionmaker_of(w.app)() as db:
        token = Token(tenant_id=w.tid, uid="04AABBCCDD", label="Radio")
        db.add(token)
        await db.commit()
        token_id = token.id
    await w.post(f"/figures/{token_id}/binding", content_id=str(content_id), next=w.url)
    assert await w.status_of("Inhalt zuordnen") == "problem"
    assert "kann die Box" in (await w.client.get(w.url)).text


@pytest.mark.parametrize(
    "target",
    ["https://evil.example/", "//evil.example/", "/t/{other}/figures", "/\\evil.example"],
)
async def test_next_stays_inside_the_household(wizard: Wizard, target: str) -> None:
    w = wizard
    target = target.replace("{other}", str(uuid.uuid4()))
    r = await w.post("/figures/adopt", uid="04DEADBEEF", label="X", next=target)
    assert r.status_code == 303
    assert r.headers["location"].startswith(f"/t/{w.tid}/figures/")


async def test_entry_points(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    home = (await client.get(f"/t/{t.tenant_id}/")).text
    assert "Erste Box einrichten" in home
    start = (await client.get(f"/t/{t.tenant_id}/setup")).text
    assert "Code eingeben" in start
    assert 'action="/t/' in start
    assert "Image herunterladen" in start
    await pair_device(app, client, t.tenant_id)
    home = (await client.get(f"/t/{t.tenant_id}/")).text
    assert "Einrichtung fortsetzen" in home
    boxes = (await client.get(f"/t/{t.tenant_id}/boxes")).text
    assert f'href="/t/{t.tenant_id}/setup"' in boxes
    r = await client.post(
        f"/t/{t.tenant_id}/boxes/add", data={"code": "000000", "name": "x", "csrf_token": csrf}
    )
    assert r.status_code == 400
    assert "ungültig oder abgelaufen" in r.text
    assert 'value="x"' in r.text  # the form keeps what was typed


async def test_viewer_sees_the_wizard_without_forms(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    await login(client, await add_member(app, t.tenant_id, Role.VIEWER))
    start = (await client.get(f"/t/{t.tenant_id}/setup")).text
    assert 'name="code"' not in start
    await Box(client, dev).event("token_unknown", {"uid": "04A1B2C3D4"})
    page = (await client.get(f"/t/{t.tenant_id}/boxes/{dev.device_id}/setup")).text
    assert "Erste Figur" in page
    assert 'name="uid"' not in page
