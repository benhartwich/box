"""Web UI flows (Jinja2 + HTMX) and revision accounting per write operation (SPEC §5.1)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import func, select

from myboxi_protocol.state import StateResponse
from myboxi_server.domain.revisions import device_rev, tenant_config_rev
from myboxi_server.jobs import context as job_context
from myboxi_server.models import Binding, Content, ContentItem, Device, Event, Token, Upload
from myboxi_server.models.enums import ContentKind, RepeatMode, Role, UploadStatus
from myboxi_server.settings import Settings

from .helpers import (
    Library,
    add_member,
    login,
    make_tenant,
    pair_device,
    seed_library,
    sessionmaker_of,
    start_pairing,
)
from .media import color_png, sine_mp3


@pytest.fixture(scope="session")
def media(tmp_path_factory: pytest.TempPathFactory) -> Path:
    d = tmp_path_factory.mktemp("uimedia")
    sine_mp3(d / "Folge 1.mp3", 2.0)
    sine_mp3(d / "Folge 2.mp3", 1.5, freq=550)
    color_png(d / "cover.png")
    return d


@dataclass
class Ui:
    app: FastAPI
    client: httpx.AsyncClient
    tid: uuid.UUID
    csrf: str
    lib: Library

    def url(self, path: str) -> str:
        return f"/t/{self.tid}{path}"

    async def post(self, path: str, **data: str) -> httpx.Response:
        return await self.client.post(self.url(path), data=data | {"csrf_token": self.csrf})

    async def config_rev(self) -> int:
        async with sessionmaker_of(self.app)() as db:
            return await tenant_config_rev(db, self.tid)


@pytest.fixture
async def ui(app: FastAPI, client: httpx.AsyncClient) -> Ui:
    t = await make_tenant(app)
    lib = await seed_library(app, t.tenant_id)
    csrf = await login(client, t.owner_email)
    return Ui(app, client, t.tenant_id, csrf, lib)


async def test_pages_render(ui: Ui) -> None:
    for path in ("/", "/boxes", "/figures", "/contents", "/members", "/contents/new?kind=podcast"):
        r = await ui.client.get(ui.url(path))
        assert r.status_code == 200, path
    r = await ui.client.get(ui.url(f"/figures/{ui.lib.token_id}"))
    assert "Folge 1" in r.text  # bound content preselected
    r = await ui.client.get(ui.url(f"/contents/{ui.lib.content_id}"))
    assert "Teil 1" in r.text
    assert 'id="tracks"' in r.text


# --- exactly one revision per write (acceptance criterion) -------------------------------

Op = Callable[[Ui], Awaitable[httpx.Response]]


async def _new_figure(ui: Ui) -> httpx.Response:
    return await ui.post("/figures", uid="04:aa:bb:cc:dd:ee:ff", label="Pumuckl")


async def _update_figure(ui: Ui) -> httpx.Response:
    return await ui.post(f"/figures/{ui.lib.token_id}", label="Bibi Blocksberg", icon="🧙")


async def _delete_figure(ui: Ui) -> httpx.Response:
    return await ui.post(f"/figures/{ui.lib.token_id}/delete")


async def _set_binding(ui: Ui) -> httpx.Response:
    return await ui.post(
        f"/figures/{ui.lib.token_id}/binding",
        content_id=str(ui.lib.content_id), resume="true", shuffle="true", repeat="all",
    )  # fmt: skip


async def _delete_binding(ui: Ui) -> httpx.Response:
    return await ui.post(f"/figures/{ui.lib.token_id}/binding/delete")


async def _new_collection(ui: Ui) -> httpx.Response:
    return await ui.post("/contents", kind="collection", title="Schlaflieder")


async def _new_podcast(ui: Ui) -> httpx.Response:
    return await ui.post(
        "/contents", kind="podcast", title="Kakadu", feed_url="https://example.org/feed.xml",
        keep_latest="3", order="newest_first",
    )  # fmt: skip


async def _new_spotify(ui: Ui) -> httpx.Response:
    return await ui.post(
        "/contents", kind="spotify", title="Album",
        uri="https://open.spotify.com/intl-de/album/4aawyAB9vmqN3uQ7FjRGTy?si=abc",
    )  # fmt: skip


async def _update_content(ui: Ui) -> httpx.Response:
    return await ui.post(f"/contents/{ui.lib.content_id}", title="Folge 1 (neu)")


async def _delete_content(ui: Ui) -> httpx.Response:
    return await ui.post(f"/contents/{ui.lib.content_id}/delete")


async def _first_item(ui: Ui) -> uuid.UUID:
    async with sessionmaker_of(ui.app)() as db:
        item = await db.scalar(
            select(ContentItem.id).where(
                ContentItem.content_id == ui.lib.content_id, ContentItem.position == 0
            )
        )
    assert item is not None
    return item


async def _move_item(ui: Ui) -> httpx.Response:
    return await ui.post(
        f"/contents/{ui.lib.content_id}/items/{await _first_item(ui)}/move", direction="down"
    )


async def _rename_item(ui: Ui) -> httpx.Response:
    return await ui.post(
        f"/contents/{ui.lib.content_id}/items/{await _first_item(ui)}/rename", title="Intro"
    )


async def _delete_item(ui: Ui) -> httpx.Response:
    return await ui.post(f"/contents/{ui.lib.content_id}/items/{await _first_item(ui)}/delete")


async def _adopt_unknown(ui: Ui) -> httpx.Response:
    return await ui.post("/figures/adopt", uid="04C0FFEE00")


async def _cover(ui: Ui, media: Path) -> httpx.Response:
    return await ui.client.post(
        ui.url(f"/contents/{ui.lib.content_id}/cover"),
        data={"csrf_token": ui.csrf},
        files={"file": ("cover.png", (media / "cover.png").read_bytes(), "image/png")},
    )


WRITE_OPS: dict[str, Op] = {
    "figure_create": _new_figure,
    "figure_update": _update_figure,
    "figure_delete": _delete_figure,
    "figure_adopt_unknown": _adopt_unknown,
    "binding_set": _set_binding,
    "binding_delete": _delete_binding,
    "collection_create": _new_collection,
    "podcast_create": _new_podcast,
    "spotify_create": _new_spotify,
    "content_update": _update_content,
    "content_delete": _delete_content,
    "item_move": _move_item,
    "item_rename": _rename_item,
    "item_delete": _delete_item,
}


@pytest.mark.parametrize("op", WRITE_OPS.keys())
async def test_every_write_raises_config_rev_exactly_once(ui: Ui, op: str) -> None:
    before = await ui.config_rev()
    r = await WRITE_OPS[op](ui)
    assert r.status_code in (200, 303), r.text
    assert await ui.config_rev() == before + 1


async def test_cover_raises_config_rev_once(ui: Ui, media: Path) -> None:
    before = await ui.config_rev()
    r = await _cover(ui, media)
    assert r.status_code == 303
    assert await ui.config_rev() == before + 1
    async with sessionmaker_of(ui.app)() as db:
        content = await db.get(Content, ui.lib.content_id)
        assert content is not None
        assert content.cover_asset_id is not None
    r = await ui.client.get(ui.url(f"/covers/{content.cover_asset_id}"))
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/jpeg"


async def test_failed_write_raises_nothing(ui: Ui) -> None:
    before = await ui.config_rev()
    r = await ui.post("/figures", uid="xyz", label="Kaputt")
    assert r.status_code == 400
    assert "Hex" in r.text
    assert await ui.config_rev() == before


# --- figures ------------------------------------------------------------------------------


async def test_figure_uid_is_normalized_and_unique(ui: Ui) -> None:
    await _new_figure(ui)
    async with sessionmaker_of(ui.app)() as db:
        assert await db.scalar(select(Token.id).where(Token.uid == "04AABBCCDDEEFF"))
    r = await ui.post("/figures", uid="04aabbccddeeff", label="Doppelt")
    assert r.status_code == 400
    assert "gibt es schon" in r.text


async def test_unknown_figures_are_listed_and_adopted(ui: Ui) -> None:
    dev = await pair_device(ui.app, ui.client, ui.tid)
    events = [
        {
            "v": 1, "id": f"01J8Z3M5W6XK2C4B7N9P0QRS{n:02d}", "ts": "2026-09-24T18:02:11Z",
            "type": "token_unknown", "boot_id": str(uuid.uuid4()), "mono_ms": n,
            "data": {"uid": uid},
        }
        for n, uid in enumerate(["04C0FFEE00", "04C0FFEE00", ui.lib.token_uid])
    ]  # fmt: skip
    await ui.client.post("/api/v1/device/events", json={"events": events}, headers=dev.auth)
    page = await ui.client.get(ui.url("/figures"))
    assert "Unbekannte Figuren" in page.text
    assert "04C0FFEE00" in page.text
    assert "2×" in page.text
    r = await _adopt_unknown(ui)
    assert r.status_code == 303
    page = await ui.client.get(ui.url("/figures"))
    assert "Unbekannte Figuren" not in page.text  # the known UID never showed up
    async with sessionmaker_of(ui.app)() as db:
        assert await db.scalar(select(func.count()).select_from(Event)) == 3


async def test_binding_options(ui: Ui) -> None:
    await _set_binding(ui)
    async with sessionmaker_of(ui.app)() as db:
        b = await db.get(Binding, ui.lib.token_id)
        assert b is not None
        assert (b.resume, b.shuffle, b.repeat) == (True, True, RepeatMode.ALL)
    # Unchecked boxes mean false.
    await ui.post(
        f"/figures/{ui.lib.token_id}/binding", content_id=str(ui.lib.content_id), repeat="off"
    )
    async with sessionmaker_of(ui.app)() as db:
        b = await db.get(Binding, ui.lib.token_id)
        assert b is not None
        assert (b.resume, b.shuffle) == (False, False)


# --- contents -----------------------------------------------------------------------------


async def test_spotify_link_is_stored_as_uri(ui: Ui) -> None:
    r = await _new_spotify(ui)
    assert r.status_code == 303
    async with sessionmaker_of(ui.app)() as db:
        c = await db.scalar(select(Content).where(Content.kind == ContentKind.SPOTIFY))
        assert c is not None
        assert c.source == {"uri": "spotify:album:4aawyAB9vmqN3uQ7FjRGTy"}
    r = await ui.post("/contents", kind="spotify", title="x", uri="https://example.org")
    assert r.status_code == 400


async def test_podcast_source(ui: Ui) -> None:
    await _new_podcast(ui)
    async with sessionmaker_of(ui.app)() as db:
        c = await db.scalar(select(Content).where(Content.kind == ContentKind.PODCAST))
        assert c is not None
        assert c.source == {
            "feed_url": "https://example.org/feed.xml",
            "keep_latest": 3,
            "order": "newest_first",
        }
    r = await ui.post(
        "/contents", kind="podcast", title="x", feed_url="ftp://nope", keep_latest="5"
    )
    assert r.status_code == 400


async def test_item_order_via_htmx(ui: Ui) -> None:
    item = await _first_item(ui)
    r = await ui.client.post(
        ui.url(f"/contents/{ui.lib.content_id}/items/{item}/move"),
        data={"direction": "down", "csrf_token": ui.csrf},
        headers={"HX-Request": "true"},
    )
    assert r.status_code == 200
    assert r.text.lstrip().startswith('<section id="tracks"')
    assert r.text.index("Teil 2") < r.text.index("Teil 1")
    await _delete_item(ui)  # removes "Teil 2" (now first); positions stay contiguous
    async with sessionmaker_of(ui.app)() as db:
        positions = (
            await db.scalars(
                select(ContentItem.position).where(ContentItem.content_id == ui.lib.content_id)
            )
        ).all()
    assert sorted(positions) == [0]


async def test_upload_through_ui_and_worker(ui: Ui, settings: Settings, media: Path) -> None:
    """Upload shows a status, the job attaches the track, the box then sees it in its state."""
    await _new_collection(ui)
    async with sessionmaker_of(ui.app)() as db:
        cid = await db.scalar(select(Content.id).where(Content.title == "Schlaflieder"))
    assert cid is not None
    r = await ui.client.post(
        ui.url(f"/contents/{cid}/uploads"),
        data={"csrf_token": ui.csrf, "profile": "speech"},
        files=[
            ("files", ("Folge 1.mp3", (media / "Folge 1.mp3").read_bytes(), "audio/mpeg")),
            ("files", ("Folge 2.mp3", (media / "Folge 2.mp3").read_bytes(), "audio/mpeg")),
            ("files", ("readme.txt", b"hello", "text/plain")),
        ],
    )
    assert r.status_code == 400  # the text file is refused, the two mp3 files are queued
    assert "readme.txt" in r.text
    assert "wird umgewandelt" in r.text or "wartet" in r.text
    assert 'hx-trigger="every 2s"' in r.text

    job_context.configure(settings)
    try:
        await ui.app.state.job_app.run_worker_async(
            queues=["media"], wait=False, install_signal_handlers=False
        )
    finally:
        await job_context.dispose()

    partial = await ui.client.get(ui.url(f"/contents/{cid}/tracks"), headers={"HX-Request": "true"})
    assert "Folge 1" in partial.text
    assert "Folge 2" in partial.text
    assert "every 2s" not in partial.text  # polling stops
    async with sessionmaker_of(ui.app)() as db:
        statuses = (await db.scalars(select(Upload.status))).all()
        assert set(statuses) == {UploadStatus.DONE}

    dev = await pair_device(ui.app, ui.client, ui.tid)
    state = StateResponse.model_validate_json(
        (await ui.client.get("/api/v1/device/state", headers=dev.auth)).content
    )
    titles = [i.title for i in state.upserts.content_item if i.content_id == cid]
    assert titles == ["Folge 1", "Folge 2"]


async def test_create_collection_with_files_in_one_step(ui: Ui, media: Path) -> None:
    """ "Dateien hochladen": title and first files in one form, no hidden second step."""
    page = await ui.client.get(ui.url("/contents"))
    assert "Dateien hochladen" in page.text
    assert "Sammlung" not in page.text
    form = await ui.client.get(ui.url("/contents/new?kind=collection"))
    assert 'enctype="multipart/form-data"' in form.text
    assert 'name="files"' in form.text
    r = await ui.client.post(
        ui.url("/contents"),
        data={"csrf_token": ui.csrf, "kind": "collection", "title": "Lieder", "profile": "music"},
        files=[("files", ("Folge 1.mp3", (media / "Folge 1.mp3").read_bytes(), "audio/mpeg"))],
    )
    assert r.status_code == 303
    cid = uuid.UUID(r.headers["location"].split("/")[-1])
    async with sessionmaker_of(ui.app)() as db:
        uploads = (await db.scalars(select(Upload).where(Upload.content_id == cid))).all()
    assert [u.original_filename for u in uploads] == ["Folge 1.mp3"]
    page = await ui.client.get(r.headers["location"])
    assert "Folge 1.mp3" in page.text


# --- boxes --------------------------------------------------------------------------------


async def test_add_box_by_code_and_configure(ui: Ui) -> None:
    started = await start_pairing(ui.client)
    r = await ui.post(
        "/boxes/add", code=f"{started.code[:3]} {started.code[3:]}", name="Kinderzimmer"
    )
    assert r.status_code == 303
    device_id = uuid.UUID(r.headers["location"].split("/")[4])
    assert r.headers["location"] == ui.url(f"/boxes/{device_id}/setup")  # into the wizard
    page = await ui.client.get(r.headers["location"])
    assert "Einrichtung: Kinderzimmer" in page.text
    page = await ui.client.get(ui.url(f"/boxes/{device_id}"))
    assert "Kinderzimmer" in page.text
    assert "noch keine Meldung" in page.text

    async with sessionmaker_of(ui.app)() as db:
        rev_before = await device_rev(db, device_id)
    r = await ui.post(
        f"/boxes/{device_id}/config",
        max_volume="60", start_volume="30", on_token_removed="continue", locale="de-DE",
        timezone="Europe/Berlin", providers="local", sleep_timer_min="30", quiet_enabled="true",
        quiet_start="19:00", quiet_end="07:00", quiet_mode="lock", quiet_max_volume="10",
    )  # fmt: skip
    assert r.status_code == 200
    assert "Einstellungen gespeichert" in r.text
    async with sessionmaker_of(ui.app)() as db:
        assert await device_rev(db, device_id) == rev_before + 1

    r = await ui.post(
        f"/boxes/{device_id}/config", max_volume="150", start_volume="30",
        on_token_removed="pause", locale="de-AT", timezone="Europe/Vienna",
    )  # fmt: skip
    assert r.status_code == 400

    r = await ui.post(f"/boxes/{device_id}/rename", name="Wohnzimmer")
    assert r.status_code == 303
    r = await ui.post(f"/boxes/{device_id}/remove")
    assert r.status_code == 303
    async with sessionmaker_of(ui.app)() as db:
        device = await db.get(Device, device_id)
        assert device is not None
        assert device.tenant_id is None


async def test_box_config_reaches_state(ui: Ui) -> None:
    dev = await pair_device(ui.app, ui.client, ui.tid)
    await ui.post(
        f"/boxes/{dev.device_id}/config",
        max_volume="45", start_volume="20", on_token_removed="pause", locale="de-AT",
        timezone="Europe/Vienna", providers="local", quiet_enabled="true", quiet_start="19:30",
        quiet_end="06:30", quiet_mode="limit", quiet_max_volume="15",
    )  # fmt: skip
    state = StateResponse.model_validate_json(
        (await ui.client.get("/api/v1/device/state", headers=dev.auth)).content
    )
    cfg = state.device_config
    assert (cfg.max_volume, cfg.start_volume, cfg.providers_enabled) == (45, 20, ["local"])
    assert cfg.quiet_hours is not None
    assert (cfg.quiet_hours.max_volume, cfg.quiet_hours.lock) == (15, None)


async def test_wrong_code_message(ui: Ui) -> None:
    r = await ui.post("/boxes/add", code="000000", name="x")
    assert r.status_code == 400
    assert "ungültig oder abgelaufen" in r.text


async def test_reported_state_is_shown(ui: Ui) -> None:
    dev = await pair_device(ui.app, ui.client, ui.tid)
    await ui.client.post(
        "/api/v1/device/reported",
        headers=dev.auth,
        json={
            "v": 1, "id": "01J8Z3M5W6XK2C4B7N9P0QRSTV", "ts": "2026-09-24T18:02:11Z",
            "type": "reported",
            "data": {
                "agent_version": "0.3.1", "hw_model": "rpi-zero2w", "applied_config_rev": 0,
                "applied_device_rev": 0, "battery": {"percent": 72, "charging": False},
                "storage": {"free_mb": 9120}, "time_trusted": False,
                "playback": {"status": "playing", "volume": 35},
                "soloist": {"installed": True, "build_expires_at": "2026-09-30"},
            },
        },
    )  # fmt: skip
    page = await ui.client.get(ui.url(f"/boxes/{dev.device_id}"))
    assert "72 %" in page.text
    assert "spielt" in page.text
    assert "ausstehend" in page.text  # applied revisions are behind
    assert "Update nötig" in page.text  # Soloist expires in < 14 days (SPEC §6.4)
    assert "unsicher" in page.text


async def test_contributor_sees_no_box_controls(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    await login(client, await add_member(app, t.tenant_id, Role.CONTRIBUTOR))
    page = await client.get(f"/t/{t.tenant_id}/boxes")
    assert "Box hinzufügen" not in page.text
    page = await client.get(f"/t/{t.tenant_id}/figures")
    assert "Figur anlegen" in page.text
