"""Spotify in the web UI with the household's own app (SPEC v0.10 §3.6), against a fake
Spotify: PKCE login, search, library, token refresh, covers."""

from __future__ import annotations

import base64
import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select

from myboxi_protocol.state import StateResponse
from myboxi_server.auth.secretbox import unseal
from myboxi_server.domain.spotify import PURPOSE, SpotifyWeb
from myboxi_server.models import Content, Device, DeviceConfig, SpotifyAccount
from myboxi_server.models.enums import Role

from .helpers import add_member, login, make_tenant, pair_device, sessionmaker_of
from .media import color_png

CLIENT_ID = "0123456789abcdef0123456789abcdef"
ALBUM = "spotify:album:4aawyAB9vmqN3uQ7FjRGTy"
PLAYLIST = "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"


def album(uri: str = ALBUM, name: str = "Bibi Blocksberg - Folge 1") -> dict[str, Any]:
    return {
        "uri": uri, "name": name, "total_tracks": 12,
        "artists": [{"name": "Bibi Blocksberg"}],
        "images": [{"url": "https://i.scdn.co/image/large", "width": 640, "height": 640},
                   {"url": "https://i.scdn.co/image/small", "width": 300, "height": 300}],
    }  # fmt: skip


def playlist() -> dict[str, Any]:
    return {"uri": PLAYLIST, "name": "Gute Nacht", "owner": {"display_name": "Mama"},
            "tracks": {"total": 7}, "images": []}  # fmt: skip


@dataclass
class FakeSpotify:
    cover: bytes
    challenges: dict[str, str] = field(default_factory=dict[str, str])
    refresh_tokens: set[str] = field(default_factory=lambda: {"refresh-1"})
    access: str = "access-1"
    rotate_to: str | None = None
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        url = request.url
        if url.host == "accounts.spotify.com":
            form = {k: v[0] for k, v in parse_qs(request.content.decode()).items()}
            assert form["client_id"] == CLIENT_ID
            assert "client_secret" not in form  # PKCE
            if form["grant_type"] == "authorization_code":
                verifier = form["code_verifier"]
                digest = hashlib.sha256(verifier.encode()).digest()
                challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode()
                if self.challenges.get(form["code"]) != challenge:
                    return httpx.Response(400, json={"error": "invalid_grant"})
                return httpx.Response(200, json={"access_token": self.access, "expires_in": 3600,
                                                 "refresh_token": "refresh-1"})  # fmt: skip
            if form["refresh_token"] not in self.refresh_tokens:
                return httpx.Response(400, json={"error": "invalid_grant"})
            body: dict[str, Any] = {"access_token": self.access, "expires_in": 3600}
            if self.rotate_to:
                body["refresh_token"] = self.rotate_to
                self.refresh_tokens = {self.rotate_to}
            return httpx.Response(200, json=body)
        if url.host == "i.scdn.co":
            return httpx.Response(200, content=self.cover)
        assert url.host == "api.spotify.com"
        if request.headers.get("authorization") != f"Bearer {self.access}":
            return httpx.Response(401)
        match url.path:
            case "/v1/me":
                return httpx.Response(200, json={"id": "mama", "display_name": "Mama"})
            case "/v1/search":
                assert url.params["type"] == "album,playlist"
                return httpx.Response(200, json={
                    "albums": {"items": [album()]},
                    "playlists": {"items": [None, playlist()]},  # Spotify sends nulls
                })  # fmt: skip
            case "/v1/me/playlists":
                return httpx.Response(200, json={"items": [playlist()]})
            case "/v1/me/albums":
                return httpx.Response(200, json={"items": [{"album": album()}]})
            case path if path.startswith("/v1/albums/"):
                return httpx.Response(200, json=album())
            case _:
                return httpx.Response(404)


@dataclass
class Rig:
    app: FastAPI
    client: httpx.AsyncClient
    tid: uuid.UUID
    csrf: str
    spotify: FakeSpotify

    def url(self, path: str) -> str:
        return f"/t/{self.tid}{path}"

    async def post(self, path: str, **data: str) -> httpx.Response:
        return await self.client.post(self.url(path), data=data | {"csrf_token": self.csrf})

    async def connect(self) -> None:
        await self.post("/spotify/app", client_id=CLIENT_ID.upper())
        r = await self.post("/spotify/connect")
        assert r.status_code == 303
        query = {k: v[0] for k, v in parse_qs(urlsplit(r.headers["location"]).query).items()}
        self.spotify.challenges["code-1"] = query["code_challenge"]
        r = await self.client.get(
            "/spotify/callback", params={"code": "code-1", "state": query["state"]}
        )
        assert r.status_code == 303, r.text


@pytest.fixture
async def rig(app: FastAPI, client: httpx.AsyncClient, tmp_path: Path) -> Rig:
    fake = FakeSpotify(cover=color_png(tmp_path / "cover.png").read_bytes())
    app.state.spotify = SpotifyWeb(httpx.AsyncClient(transport=httpx.MockTransport(fake.handler)))
    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    return Rig(app, client, t.tenant_id, csrf, fake)


async def test_the_page_explains_the_setup(rig: Rig) -> None:
    page = await rig.client.get(rig.url("/spotify"))
    assert "developer.spotify.com/dashboard" in page.text
    assert "/spotify/callback" in page.text  # the redirect URI to copy
    r = await rig.post("/spotify/app", client_id="nicht-gueltig")
    assert "32 Zeichen" in (await rig.client.get(r.headers["location"])).text


async def test_spotify_is_easy_to_find_and_shows_each_box(rig: Rig) -> None:
    """The setup is linked from the contents page; the page says what each box still needs."""
    page = await rig.client.get(rig.url("/contents"))
    assert f'href="/t/{rig.tid}/spotify"' in page.text
    assert "Spotify einrichten" in page.text
    off = await pair_device(rig.app, rig.client, rig.tid)
    on = await pair_device(rig.app, rig.client, rig.tid)
    reported = {"agent_version": "0.7.0", "hw_model": "rpi-zero2w", "applied_config_rev": 1,
                "applied_device_rev": 1, "storage": {"free_mb": 100}, "time_trusted": True,
                "playback": {"status": "stopped", "volume": 30},
                "soloist": {"installed": False, "state": "no_key"}}  # fmt: skip
    async with sessionmaker_of(rig.app)() as db:
        device = await db.get(Device, on.device_id)
        assert device is not None
        device.reported = reported
        config = await db.scalar(select(DeviceConfig).where(DeviceConfig.device_id == on.device_id))
        assert config is not None
        config.providers_enabled = [*config.providers_enabled, "spotify"]
        await db.commit()
    page = await rig.client.get(rig.url("/spotify"))
    assert "Spotify Soloist API Key" in page.text  # the box part comes first
    assert f"/boxes/{off.device_id}" in page.text
    assert "Spotify ist aus" in page.text
    assert "Der Spotify-Schlüssel fehlt" in page.text
    box = await rig.client.get(rig.url(f"/boxes/{on.device_id}"))
    assert f'href="/t/{rig.tid}/spotify"' in box.text


async def test_pkce_login_stores_the_refresh_token_sealed(rig: Rig) -> None:
    await rig.post("/spotify/app", client_id=CLIENT_ID)
    r = await rig.post("/spotify/connect")
    location = urlsplit(r.headers["location"])
    query = {k: v[0] for k, v in parse_qs(location.query).items()}
    assert (location.hostname, query["client_id"]) == ("accounts.spotify.com", CLIENT_ID)
    assert query["code_challenge_method"] == "S256"
    assert query["redirect_uri"].endswith("/spotify/callback")
    assert "playlist-read-private" in query["scope"]
    rig.spotify.challenges["code-1"] = query["code_challenge"]
    r = await rig.client.get(
        "/spotify/callback", params={"code": "code-1", "state": query["state"]}
    )
    assert r.headers["location"].startswith(f"/t/{rig.tid}/spotify")
    page = await rig.client.get(r.headers["location"])
    assert "Mama" in page.text
    async with sessionmaker_of(rig.app)() as db:
        account = await db.get(SpotifyAccount, rig.tid)
        assert account is not None
        assert account.refresh_token is not None
        assert b"refresh-1" not in account.refresh_token  # never stored in plain text
        assert unseal(rig.app.state.settings, PURPOSE, account.refresh_token) == "refresh-1"
    # the state works once only
    r = await rig.client.get(
        "/spotify/callback", params={"code": "code-1", "state": query["state"]}
    )
    assert r.status_code == 400


async def test_a_cancelled_login(rig: Rig) -> None:
    r = await rig.client.get("/spotify/callback", params={"error": "access_denied", "state": "x"})
    assert r.status_code == 400
    assert "abgebrochen" in r.text


async def test_search_and_add_with_title_and_cover(rig: Rig) -> None:
    await rig.connect()
    page = await rig.client.get(rig.url("/contents/new?kind=spotify"))
    assert 'hx-get="/t/' in page.text  # the search field
    r = await rig.client.get(rig.url("/spotify/search"), params={"q": "bibi"})
    assert "Bibi Blocksberg - Folge 1" in r.text
    assert "Playlist von Mama" in r.text
    assert "12 Titel" in r.text
    r = await rig.post("/spotify/add", uri=ALBUM, title="Bibi Blocksberg - Folge 1",
                       image="https://i.scdn.co/image/large")  # fmt: skip
    assert r.status_code == 303
    async with sessionmaker_of(rig.app)() as db:
        content = await db.scalar(select(Content).where(Content.tenant_id == rig.tid))
        assert content is not None
        assert (content.title, content.source) == ("Bibi Blocksberg - Folge 1", {"uri": ALBUM})
        assert content.cover_asset_id is not None


async def test_the_box_gets_only_the_uri(rig: Rig) -> None:
    """SPEC §3.6, §8.1: the box never talks to the Web API."""
    await rig.connect()
    await rig.post("/spotify/add", uri=ALBUM, title="Album", image="")
    dev = await pair_device(rig.app, rig.client, rig.tid)
    state = StateResponse.model_validate_json(
        (await rig.client.get("/api/v1/device/state", headers=dev.auth)).content
    )
    (content,) = state.upserts.content
    assert content.model_dump(mode="json")["source"] == {"uri": ALBUM}


async def test_library_lists(rig: Rig) -> None:
    await rig.connect()
    r = await rig.client.get(rig.url("/spotify/library"), params={"what": "playlists"})
    assert "Gute Nacht" in r.text
    r = await rig.client.get(rig.url("/spotify/library"), params={"what": "albums"})
    assert "Bibi Blocksberg - Folge 1" in r.text


async def test_pasted_link_gets_title_and_cover_from_spotify(rig: Rig) -> None:
    await rig.connect()
    link = "https://open.spotify.com/album/4aawyAB9vmqN3uQ7FjRGTy?si=x"
    r = await rig.post("/contents", kind="spotify", title="", uri=link)
    assert r.status_code == 303
    async with sessionmaker_of(rig.app)() as db:
        content = await db.scalar(select(Content).where(Content.tenant_id == rig.tid))
        assert content is not None
        assert content.title == "Bibi Blocksberg - Folge 1"
        assert content.cover_asset_id is not None


async def test_pasted_link_without_connection_needs_a_title(rig: Rig) -> None:
    r = await rig.post("/contents", kind="spotify", title="", uri=ALBUM)
    assert r.status_code == 400
    assert "Titel" in r.text
    r = await rig.post("/contents", kind="spotify", title="Schlaflieder", uri=ALBUM)
    assert r.status_code == 303


async def test_expired_access_token_is_refreshed_and_rotation_kept(rig: Rig) -> None:
    await rig.connect()
    web: SpotifyWeb = rig.app.state.spotify
    web.forget_tokens()  # as after a server restart
    rig.spotify.access, rig.spotify.rotate_to = "access-2", "refresh-2"
    r = await rig.client.get(rig.url("/spotify/search"), params={"q": "bibi"})
    assert "Bibi Blocksberg" in r.text
    async with sessionmaker_of(rig.app)() as db:
        account = await db.get(SpotifyAccount, rig.tid)
        assert account is not None
        assert account.refresh_token is not None
        assert unseal(rig.app.state.settings, PURPOSE, account.refresh_token) == "refresh-2"


async def test_revoked_access_asks_to_reconnect(rig: Rig) -> None:
    await rig.connect()
    web: SpotifyWeb = rig.app.state.spotify
    web.forget_tokens()  # as after a server restart
    rig.spotify.refresh_tokens = set()  # revoked in the Spotify account
    r = await rig.client.get(rig.url("/spotify/search"), params={"q": "bibi"})
    assert "neu verbinden" in r.text
    page = await rig.client.get(rig.url("/contents/new?kind=spotify"))
    assert "Spotify verbinden</a>" in page.text  # back to links until reconnected


async def test_covers_only_from_spotify(rig: Rig) -> None:
    await rig.connect()
    await rig.post("/spotify/add", uri=ALBUM, title="Album", image="http://10.0.0.1/x.jpg")
    assert all(r.url.host != "10.0.0.1" for r in rig.spotify.requests)


async def test_contributor_searches_but_cannot_connect(
    app: FastAPI, client: httpx.AsyncClient, rig: Rig
) -> None:
    await rig.connect()
    email = await add_member(app, rig.tid, Role.CONTRIBUTOR)
    await rig.client.post("/logout", data={"csrf_token": rig.csrf})
    csrf = await login(client, email)
    r = await client.post(rig.url("/spotify/connect"), data={"csrf_token": csrf})
    assert r.status_code == 403
    r = await client.get(rig.url("/spotify/search"), params={"q": "bibi"})
    assert "Bibi Blocksberg" in r.text


async def test_disconnect(rig: Rig) -> None:
    await rig.connect()
    await rig.post("/spotify/disconnect")
    async with sessionmaker_of(rig.app)() as db:
        assert await db.get(SpotifyAccount, rig.tid) is None
