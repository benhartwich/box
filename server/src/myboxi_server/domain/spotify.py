"""Spotify in the web UI: the household's own app, search and library (SPEC v0.10 §3.6).

Parents pick albums and playlists here instead of copying links; the box never talks to the
Web API and only gets the URI (SPEC §8.1, CLAUDE.md). Each household registers its own
Spotify app (development mode: 5 users, owner with Premium) and gives us its client id;
Authorization Code with PKCE needs no client secret. The refresh token is stored sealed; access
tokens live only in memory.
"""

from __future__ import annotations

import base64
import datetime as dt
import hashlib
import logging
import re
import secrets
import time
import uuid
from dataclasses import dataclass
from typing import Any, Literal, cast
from urllib.parse import urlencode, urlsplit

import httpx
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_server.auth.secretbox import seal, unseal
from myboxi_server.domain.authz import Perm, TenantContext
from myboxi_server.domain.errors import DomainError, InvalidInputError, NotFoundError
from myboxi_server.models import SpotifyAccount, SpotifyLogin
from myboxi_server.settings import Settings

log = logging.getLogger(__name__)

AUTHORIZE_URL = "https://accounts.spotify.com/authorize"
TOKEN_URL = "https://accounts.spotify.com/api/token"
API = "https://api.spotify.com/v1"
SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"
LOGIN_TTL = dt.timedelta(minutes=10)
PURPOSE = "spotify-refresh"
CLIENT_ID = re.compile(r"^[0-9a-f]{32}$")
IMAGE_HOSTS = (".scdn.co", ".spotifycdn.com")
MAX_IMAGE = 5 * 1024 * 1024
ItemKind = Literal["album", "playlist"]


class SpotifyError(DomainError):
    """A German message for the page; the connection may need renewing."""


class NotConnectedError(SpotifyError):
    pass


def callback_url(settings: Settings) -> str:
    """The redirect URI families enter in their Spotify app."""
    return f"{settings.base_url.rstrip('/')}/spotify/callback"


def _hash(state: str) -> bytes:
    return hashlib.sha256(state.encode()).digest()


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


# --- the household's app -------------------------------------------------------------------


async def get_account(db: AsyncSession, ctx: TenantContext) -> SpotifyAccount | None:
    ctx.require(Perm.READ)
    return await db.get(SpotifyAccount, ctx.tenant_id)


async def save_client_id(db: AsyncSession, ctx: TenantContext, raw: str) -> SpotifyAccount:
    ctx.require(Perm.SPOTIFY_CONNECT)
    client_id = raw.strip().lower()
    if not CLIENT_ID.fullmatch(client_id):
        raise InvalidInputError(
            "Die Client-ID hat 32 Zeichen (Ziffern und a–f). Sie steht im Spotify-Entwickler"
            "portal bei deiner App unter „Basic Information“."
        )
    account = await db.get(SpotifyAccount, ctx.tenant_id)
    if account is None:
        account = SpotifyAccount(tenant_id=ctx.tenant_id, client_id=client_id)
        db.add(account)
    elif account.client_id != client_id:
        account.client_id = client_id
        account.refresh_token = None  # a token belongs to the app it was issued for
        account.display_name = None
        account.connected_at = None
    await db.flush()
    return account


async def start_login(
    db: AsyncSession, ctx: TenantContext, settings: Settings, *, now: dt.datetime | None = None
) -> str:
    """The Spotify URL to open; the state is single-use and bound to this user."""
    ctx.require(Perm.SPOTIFY_CONNECT)
    account = await db.get(SpotifyAccount, ctx.tenant_id)
    if account is None or ctx.user_id is None:
        raise InvalidInputError("Bitte zuerst die Client-ID deiner Spotify-App eintragen.")
    now = now or dt.datetime.now(dt.UTC)
    await db.execute(delete(SpotifyLogin).where(SpotifyLogin.expires_at < now))
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    db.add(
        SpotifyLogin(
            state_hash=_hash(state),
            tenant_id=ctx.tenant_id,
            user_id=ctx.user_id,
            code_verifier=verifier,
            expires_at=now + LOGIN_TTL,
        )
    )
    await db.flush()
    query = {
        "client_id": account.client_id,
        "response_type": "code",
        "redirect_uri": callback_url(settings),
        "scope": SCOPES,
        "code_challenge_method": "S256",
        "code_challenge": _challenge(verifier),
        "state": state,
    }
    return f"{AUTHORIZE_URL}?{urlencode(query)}"


# --- items ---------------------------------------------------------------------------------


@dataclass(frozen=True)
class Item:
    kind: ItemKind
    uri: str
    name: str
    subtitle: str
    image: str | None  # small, for the list
    image_large: str | None  # for the cover
    tracks: int | None


Json = dict[str, Any]


def _dict(value: object) -> Json:
    return cast(Json, value) if isinstance(value, dict) else {}


def _list(value: object) -> list[object]:
    return cast(list[object], value) if isinstance(value, list) else []


def _str(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _int(value: object) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _images(obj: Json) -> tuple[str | None, str | None]:
    images = [(_int(d.get("width")) or 0, url) for d in map(_dict, _list(obj.get("images")))
              if (url := _str(d.get("url")))]  # fmt: skip
    if not images:
        return None, None
    images.sort()
    small = next((url for width, url in images if width >= 150), images[-1][1])
    return small, images[-1][1]


def _item(value: object, kind: ItemKind) -> Item | None:
    obj = _dict(value)  # Spotify lists may contain null entries
    uri, name = _str(obj.get("uri")), _str(obj.get("name"))
    if uri is None or name is None or not uri.startswith(f"spotify:{kind}:"):
        return None
    small, large = _images(obj)
    if kind == "album":
        artists = [_str(_dict(a).get("name")) for a in _list(obj.get("artists"))]
        subtitle = ", ".join(a for a in artists if a)
        tracks = _int(obj.get("total_tracks"))
    else:
        owner = _str(_dict(obj.get("owner")).get("display_name")) or "Spotify"
        subtitle = f"Playlist von {owner}"
        counts = _dict(obj.get("tracks") or obj.get("items"))
        tracks = _int(counts.get("total"))
    return Item(kind, uri, name[:200], subtitle[:200], small, large, tracks)


def parse_link(raw: str) -> tuple[ItemKind, str] | None:
    """``spotify:album:ID`` or an open.spotify.com link to an album or playlist."""
    value = raw.strip()
    m = re.fullmatch(r"spotify:(album|playlist):([0-9A-Za-z]{22})", value) or re.match(
        r"^https://open\.spotify\.com/(?:intl-[a-z]{2}/)?(album|playlist)/([0-9A-Za-z]{22})", value
    )
    if m is None:
        return None
    kind: ItemKind = "album" if m.group(1) == "album" else "playlist"
    return kind, m.group(2)


# --- Web API -------------------------------------------------------------------------------


class SpotifyWeb:
    """Web API client with an in-memory access token per household."""

    def __init__(self, http: httpx.AsyncClient | None = None) -> None:
        self.http = http or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        self._tokens: dict[uuid.UUID, tuple[str, float]] = {}

    async def aclose(self) -> None:
        await self.http.aclose()

    def forget_tokens(self) -> None:
        """Drop the cached access tokens (they are only an optimisation)."""
        self._tokens.clear()

    async def finish_login(
        self,
        db: AsyncSession,
        settings: Settings,
        *,
        user_id: uuid.UUID,
        state: str,
        code: str,
        now: dt.datetime | None = None,
    ) -> uuid.UUID:
        """Exchange the code (PKCE) and store the sealed refresh token. Returns the tenant."""
        now = now or dt.datetime.now(dt.UTC)
        login = await db.get(SpotifyLogin, _hash(state))
        if login is None or login.expires_at < now or login.user_id != user_id:
            raise SpotifyError("Die Anmeldung bei Spotify ist abgelaufen. Bitte noch einmal.")
        await db.delete(login)
        account = await db.get(SpotifyAccount, login.tenant_id)
        if account is None:
            raise NotFoundError()
        data = await self._token_request(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": callback_url(settings),
                "client_id": account.client_id,
                "code_verifier": login.code_verifier,
            }
        )
        refresh = _str(data.get("refresh_token"))
        if refresh is None:
            raise SpotifyError("Spotify hat keine dauerhafte Anmeldung geliefert.")
        account.refresh_token = seal(settings, PURPOSE, refresh)
        account.connected_at, account.connected_by = now, user_id
        self._remember(login.tenant_id, data)
        me = await self._get_with(str(data["access_token"]), "/me", {})
        name = _str(me.get("display_name")) or _str(me.get("id"))
        account.display_name = name[:200] if name else None
        await db.flush()
        return login.tenant_id

    async def disconnect(self, db: AsyncSession, ctx: TenantContext) -> None:
        ctx.require(Perm.SPOTIFY_CONNECT)
        self._tokens.pop(ctx.tenant_id, None)
        await db.execute(delete(SpotifyAccount).where(SpotifyAccount.tenant_id == ctx.tenant_id))

    async def search(
        self, db: AsyncSession, ctx: TenantContext, settings: Settings, query: str
    ) -> list[Item]:
        ctx.require(Perm.CONTENT_WRITE)
        q = " ".join(query.split())[:100]
        if not q:
            return []
        data = await self._get(
            db, ctx, settings, "/search",
            {"q": q, "type": "album,playlist", "limit": 12, "market": "from_token"},
        )  # fmt: skip
        albums = [_item(a, "album") for a in _list(_dict(data.get("albums")).get("items"))]
        lists = [_item(p, "playlist") for p in _list(_dict(data.get("playlists")).get("items"))]
        return [i for i in albums + lists if i is not None]

    async def library(
        self,
        db: AsyncSession,
        ctx: TenantContext,
        settings: Settings,
        what: Literal["playlists", "albums"],
    ) -> list[Item]:
        ctx.require(Perm.CONTENT_WRITE)
        if what == "playlists":
            data = await self._get(db, ctx, settings, "/me/playlists", {"limit": 50})
            found = [_item(p, "playlist") for p in _list(data.get("items"))]
        else:
            data = await self._get(db, ctx, settings, "/me/albums", {"limit": 50})
            found = [_item(_dict(e).get("album"), "album") for e in _list(data.get("items"))]
        return [i for i in found if i is not None]

    async def lookup(
        self, db: AsyncSession, ctx: TenantContext, settings: Settings, link: str
    ) -> Item | None:
        """Title and cover for a pasted link (None if it is not an album or playlist)."""
        ctx.require(Perm.CONTENT_WRITE)
        parsed = parse_link(link)
        if parsed is None:
            return None
        kind, spotify_id = parsed
        path = f"/{kind}s/{spotify_id}"
        params = {"market": "from_token"} if kind == "album" else {}
        return _item(await self._get(db, ctx, settings, path, params), kind)

    async def cover(self, url: str) -> bytes | None:
        """The cover image, only from Spotify's image hosts, at most 5 MB."""
        parts = urlsplit(url)
        if parts.scheme != "https" or not (parts.hostname or "").endswith(IMAGE_HOSTS):
            return None
        try:
            r = await self.http.get(url)
        except httpx.HTTPError:
            return None
        if r.status_code != 200 or len(r.content) > MAX_IMAGE:
            return None
        return r.content

    # --- tokens -----------------------------------------------------------------------------

    def _remember(self, tenant_id: uuid.UUID, data: Json) -> None:
        expires = float(_int(data.get("expires_in")) or 3600)
        self._tokens[tenant_id] = (str(data["access_token"]), time.monotonic() + expires - 60)

    async def _token_request(self, form: dict[str, str]) -> dict[str, Any]:
        try:
            r = await self.http.post(TOKEN_URL, data=form)
        except httpx.HTTPError as exc:
            raise SpotifyError("Spotify ist gerade nicht erreichbar.") from exc
        if r.status_code in (400, 401):
            raise NotConnectedError(
                "Die Verbindung zu Spotify ist abgelaufen oder wurde aufgehoben. Bitte neu "
                "verbinden."
            )
        if r.status_code != 200:
            raise SpotifyError("Spotify ist gerade nicht erreichbar.")
        data = _dict(r.json())
        if _str(data.get("access_token")) is None:
            raise SpotifyError("Spotify hat keine Anmeldung geliefert.")
        return data

    async def _access(
        self, db: AsyncSession, tenant_id: uuid.UUID, settings: Settings, *, fresh: bool = False
    ) -> str:
        cached = self._tokens.get(tenant_id)
        if cached is not None and not fresh and time.monotonic() < cached[1]:
            return cached[0]
        account = await db.get(SpotifyAccount, tenant_id)
        refresh = (
            unseal(settings, PURPOSE, account.refresh_token)
            if account is not None and account.refresh_token
            else None
        )
        if account is None or refresh is None:
            raise NotConnectedError("Spotify ist für diesen Haushalt nicht verbunden.")
        try:
            data = await self._token_request(
                {"grant_type": "refresh_token", "refresh_token": refresh,
                 "client_id": account.client_id}
            )  # fmt: skip
        except NotConnectedError:
            account.refresh_token = None
            await db.flush()
            raise
        if rotated := _str(data.get("refresh_token")):  # Spotify may rotate it
            account.refresh_token = seal(settings, PURPOSE, rotated)
            await db.flush()
        self._remember(tenant_id, data)
        return str(data["access_token"])

    async def _get(
        self,
        db: AsyncSession,
        ctx: TenantContext,
        settings: Settings,
        path: str,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        token = await self._access(db, ctx.tenant_id, settings)
        try:
            return await self._get_with(token, path, params)
        except _Unauthorized:
            token = await self._access(db, ctx.tenant_id, settings, fresh=True)
            return await self._get_with(token, path, params)

    async def _get_with(self, token: str, path: str, params: dict[str, Any]) -> dict[str, Any]:
        try:
            r = await self.http.get(
                API + path, params=params, headers={"Authorization": f"Bearer {token}"}
            )
        except httpx.HTTPError as exc:
            raise SpotifyError("Spotify ist gerade nicht erreichbar.") from exc
        if r.status_code == 401:
            raise _Unauthorized
        if r.status_code == 403:
            raise SpotifyError(
                "Spotify lässt diese Anmeldung nicht zu. Ist dein Spotify-Konto in der App unter "
                "„User Management“ eingetragen, und hat der Besitzer der App Premium?"
            )
        if r.status_code == 404:
            raise SpotifyError("Das findet Spotify nicht (manche Spotify-Playlists sind gesperrt).")
        if r.status_code == 429:
            raise SpotifyError("Spotify bremst gerade. Bitte kurz warten.")
        if r.status_code != 200:
            raise SpotifyError("Spotify ist gerade nicht erreichbar.")
        return _dict(r.json())


class _Unauthorized(Exception):
    pass
