"""Podcast provider (SPEC v0.8 §8.2, §4.1, §3.10)."""

from __future__ import annotations

import datetime as dt
import hashlib
import shutil
import uuid
from collections.abc import AsyncIterator, Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from myboxi_agent.adapters.loudness import MpvLoudness, gain_for, integrated_loudness
from myboxi_agent.core.clock import FakeClock
from myboxi_agent.core.model import Loading, PlanItem, Playable, ResumePoint, Unavailable
from myboxi_agent.providers.feed import (
    MAX_ENTRIES,
    FeedError,
    episode_key,
    parse_duration,
    parse_feed,
)
from myboxi_agent.providers.netguard import feed_client, is_public
from myboxi_agent.providers.podcast import PodcastRefresher, episode_suffix
from myboxi_agent.store.db import connect
from myboxi_agent.store.repos import (
    AssetRepo,
    Database,
    LibraryRepo,
    ResumeRepo,
    StateRepo,
)
from myboxi_protocol.state import DeviceConfig, StateResponse

UID = "04A2B3C4D5E680"
FEED_URL = "https://podcast.example.org/feed.xml"
ITUNES = 'xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"'


def rss(*items: str, channel: str = "") -> bytes:
    return (
        f'<?xml version="1.0" encoding="UTF-8"?><rss version="2.0" {ITUNES}><channel>'
        f"<title>Kinderpodcast</title>{channel}{''.join(items)}</channel></rss>"
    ).encode()


def item(
    n: int, day: int | None = None, *, explicit: str | None = None, mime: str = "audio/mpeg"
) -> str:
    date = f"<pubDate>{day:02d} Sep 2026 06:00:00 +0000</pubDate>" if day else ""
    flag = f"<itunes:explicit>{explicit}</itunes:explicit>" if explicit else ""
    return (
        f"<item><title>Folge {n}</title><guid>ep-{n}</guid>{date}{flag}"
        f'<enclosure url="https://cdn.example.org/{n}.mp3" type="{mime}" length="3000"/>'
        f"<itunes:duration>01:02</itunes:duration></item>"
    )


# --- feed ----------------------------------------------------------------------------------


def test_rss_episodes_newest_first_without_explicit_and_video() -> None:
    feed = parse_feed(
        rss(item(1, 1), item(3, 3), item(2, 2, explicit="yes"), item(4, 4, mime="video/mp4"))
    )
    assert [e.title for e in feed.episodes] == ["Folge 1", "Folge 3", "Folge 2", "Folge 4"]
    assert [e.title for e in feed.eligible()] == ["Folge 3", "Folge 1"]
    first = feed.eligible()[0]
    assert first.key == episode_key("ep-3", "https://cdn.example.org/3.mp3")
    assert first.duration_ms == 62_000
    assert first.published_at == dt.datetime(2026, 9, 3, 6, tzinfo=dt.UTC)


def test_explicit_channel_marks_every_episode_unless_the_episode_says_clean() -> None:
    feed = parse_feed(
        rss(
            item(1, 1),
            item(2, 2, explicit="clean"),
            channel="<itunes:explicit>true</itunes:explicit>",
        )
    )
    assert [e.title for e in feed.eligible()] == ["Folge 2"]


def test_undated_episodes_keep_feed_order_after_dated_ones() -> None:
    feed = parse_feed(rss(item(1), item(2), item(3, 5)))
    assert [e.title for e in feed.eligible()] == ["Folge 3", "Folge 1", "Folge 2"]


def test_atom_enclosures() -> None:
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom">
    <entry><id>urn:a</id><title>Atom 1</title><published>2026-09-20T10:00:00Z</published>
    <link rel="alternate" href="https://example.org/1"/>
    <link rel="enclosure" type="audio/ogg" href="https://cdn.example.org/a.ogg" length="99"/>
    </entry></feed>"""
    (episode,) = parse_feed(atom).episodes
    assert (episode.title, episode.url, episode.mime) == (
        "Atom 1", "https://cdn.example.org/a.ogg", "audio/ogg",
    )  # fmt: skip
    assert episode.key == episode_key("urn:a", episode.url)


def test_key_falls_back_to_the_url_without_guid() -> None:
    feed = parse_feed(rss('<item><enclosure url="https://c.example.org/x.mp3"/></item>'))
    assert feed.episodes[0].key == hashlib.sha256(b"https://c.example.org/x.mp3").hexdigest()[:32]
    assert feed.episodes[0].title == "Folge"


@pytest.mark.parametrize(
    "doc",
    [
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel/></rss>',
        b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/passwd">]><rss>&x;</rss>',
        b"<html><body>not a feed</body></html>",
        b"<rss><channel><item>",
    ],
)
def test_invalid_or_hostile_documents_are_rejected(doc: bytes) -> None:
    with pytest.raises(FeedError):
        parse_feed(doc)


def test_entry_limit() -> None:
    with pytest.raises(FeedError):
        parse_feed(rss(*["<item/>"] * (MAX_ENTRIES + 1)))


def test_enclosures_must_be_http() -> None:
    feed = parse_feed(rss('<item><enclosure url="file:///etc/passwd"/></item>'))
    assert feed.episodes == []


@pytest.mark.parametrize(
    ("text", "ms"), [("62", 62_000), ("1:02", 62_000), ("1:00:01", 3_601_000), ("x", 0), ("", 0)]
)
def test_duration(text: str, ms: int) -> None:
    assert parse_duration(text) == ms


def test_episode_suffix() -> None:
    assert episode_suffix("audio/mp4", "https://x/y") == ".m4a"
    assert episode_suffix(None, "https://x/y.OGG?a=1") == ".ogg"
    assert episode_suffix("application/octet-stream", "https://x/y.exe") == ".mp3"


# --- addresses -----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("address", "public"),
    [
        ("93.184.215.14", True), ("2a00:1450:4001:80b::200e", True),
        ("127.0.0.1", False), ("10.1.2.3", False), ("192.168.1.1", False), ("172.16.0.1", False),
        ("169.254.169.254", False), ("100.64.0.1", False), ("0.0.0.0", False),  # noqa: S104
        ("::1", False),
        ("fe80::1", False), ("fd00::1", False), ("224.0.0.251", False), ("::ffff:127.0.0.1", False),
    ],
)  # fmt: skip
def test_public_addresses(address: str, public: bool) -> None:
    import ipaddress

    assert is_public(ipaddress.ip_address(address)) is public


@pytest.mark.parametrize(
    "url",
    ["http://127.0.0.1:9/f.xml", "http://localhost:9/f.xml", "http://[::1]:9/", "http://10.0.0.1/"],
)
async def test_feed_client_refuses_local_addresses(url: str) -> None:
    """SPEC v0.8 §8.2, checked where the connection opens (also after redirects)."""
    async with feed_client() as http:
        with pytest.raises(httpx.ConnectError, match="not a public address"):
            await http.get(url)


# --- store and resolution ------------------------------------------------------------------


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(connect(tmp_path / "myboxi.db"), tmp_path / "assets", FakeClock())


def podcast_snapshot(
    *, keep_latest: int = 2, order: str = "newest_first", content_id: uuid.UUID | None = None
) -> StateResponse:
    token_id, content_id = uuid.uuid4(), content_id or uuid.uuid4()
    return StateResponse.model_validate(
        {
            "full": True, "config_rev": 4, "device_rev": 1,
            "upserts": {
                "token": [{"id": str(token_id), "uid": UID, "label": "Bibi"}],
                "content": [{
                    "id": str(content_id), "kind": "podcast", "title": "Kinderpodcast", "rev": 1,
                    "source": {"feed_url": FEED_URL, "keep_latest": keep_latest, "order": order},
                }],
                "binding": [{"token_id": str(token_id), "content_id": str(content_id)}],
            },
            "device_config": {},
        }
    )  # fmt: skip


class FeedServer:
    """httpx mock: a feed with ETag, enclosures with Range support."""

    def __init__(self, feed: bytes) -> None:
        self.feed = feed
        self.files: dict[str, bytes] = {
            f"/{n}.mp3": f"episode {n} ".encode() * 300 for n in range(1, 10)
        }
        self.requests: list[httpx.Request] = []
        self.feed_status = 200
        self.cut_after: int | None = None  # simulate a dropped connection

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if request.url.host == "podcast.example.org":
            if self.feed_status != 200:
                return httpx.Response(self.feed_status)
            etag = '"' + hashlib.sha256(self.feed).hexdigest()[:8] + '"'
            if request.headers.get("if-none-match") == etag:
                return httpx.Response(304)
            return httpx.Response(200, content=self.feed, headers={"ETag": etag})
        body = self.files.get(request.url.path)
        if body is None:
            return httpx.Response(404)
        start = 0
        if rng := request.headers.get("range"):
            start = int(rng.removeprefix("bytes=").removesuffix("-"))
        chunk = body[start:]
        if self.cut_after is not None:
            chunk = chunk[: self.cut_after]
            self.cut_after = None

            async def broken() -> AsyncIterator[bytes]:
                yield chunk
                raise httpx.ReadError("connection lost")

            return httpx.Response(206 if start else 200, content=broken())
        return httpx.Response(206 if start else 200, content=chunk)


class FakeLoudness:
    def __init__(self, lufs: float | None) -> None:
        self.lufs = lufs
        self.measured: list[Path] = []

    async def measure(self, path: Path) -> float | None:
        self.measured.append(path)
        return self.lufs


def refresher(
    db: Database,
    server: FeedServer,
    *,
    free: int = 10**10,
    busy: Callable[[], bool] = lambda: False,
    loudness: FakeLoudness | None = None,
    outbox: Any = None,
) -> PodcastRefresher:
    lib = LibraryRepo(db)
    return PodcastRefresher(
        repo=lib.podcasts,
        assets=AssetRepo(db),
        clock=db.clock,
        http_factory=lambda: httpx.AsyncClient(transport=httpx.MockTransport(server.handler)),
        outbox=outbox,
        disk_free=lambda: free,
        enabled=lambda: "podcast" in StateRepo(db).device_config().providers_enabled,
        busy=busy,
        loudness=loudness,
    )


async def test_new_podcast_loads_then_plays_newest_episodes(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot(keep_latest=2))
    assert isinstance(lib.resolve(UID), Loading)  # SPEC v0.8 §8.2: not read yet
    server = FeedServer(rss(item(1, 1), item(2, 2), item(3, 3)))
    await refresher(db, server).refresh_once()
    plan = lib.resolve(UID)
    assert isinstance(plan, Playable)
    assert plan.provider == "podcast"
    assert [i.title for i in plan.items] == ["Folge 3", "Folge 2"]
    assert plan.items[0].key == episode_key("ep-3", "https://cdn.example.org/3.mp3")
    assert plan.items[0].source.endswith(".mp3")
    assert Path(plan.items[0].source).read_bytes() == server.files["/3.mp3"]
    assert [r.url.path for r in server.requests] == ["/feed.xml", "/3.mp3", "/2.mp3"]


async def test_oldest_first_plays_the_kept_episodes_in_order(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot(keep_latest=2, order="oldest_first"))
    await refresher(db, FeedServer(rss(item(1, 1), item(2, 2), item(3, 3)))).refresh_once()
    plan = lib.resolve(UID)
    assert isinstance(plan, Playable)
    assert [i.title for i in plan.items] == ["Folge 2", "Folge 3"]


async def test_new_episode_moves_the_window_and_old_one_becomes_evictable(db: Database) -> None:
    lib, assets = LibraryRepo(db), AssetRepo(db)
    lib.activate(podcast_snapshot(keep_latest=1))
    server = FeedServer(rss(item(1, 1)))
    r = refresher(db, server)
    await r.refresh_once()
    first = lib.resolve(UID)
    assert isinstance(first, Playable)
    old_sha = hashlib.sha256(server.files["/1.mp3"]).hexdigest()
    assert old_sha in assets.referenced()
    server.feed = rss(item(1, 1), item(2, 2))
    db.clock.advance(7 * 3600)  # type: ignore[attr-defined]
    await r.refresh_once()
    plan = lib.resolve(UID)
    assert isinstance(plan, Playable)
    assert [i.title for i in plan.items] == ["Folge 2"]
    assert old_sha not in assets.referenced()
    assert old_sha in [sha for sha, _ in assets.evictable()]


async def test_conditional_get_and_refresh_interval(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot())
    server = FeedServer(rss(item(1, 1)))
    r = refresher(db, server)
    await r.refresh_once()
    await r.refresh_once()  # not due yet: no request
    assert [q.url.path for q in server.requests] == ["/feed.xml", "/1.mp3"]
    db.clock.advance(6.6 * 3600)  # type: ignore[attr-defined]
    await r.refresh_once()
    assert server.requests[-1].headers["if-none-match"].startswith('"')
    assert len(server.requests) == 3  # 304: nothing downloaded again
    assert isinstance(lib.resolve(UID), Playable)


async def test_feed_error_without_episodes_is_reported_on_placing(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot())
    server = FeedServer(b"")
    server.feed_status = 404
    await refresher(db, server).refresh_once()
    res = lib.resolve(UID)
    assert isinstance(res, Unavailable)
    assert (res.provider, res.code) == ("podcast", "feed_error")


async def test_invalid_feed_is_a_feed_error(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot())
    await refresher(db, FeedServer(b"<html>Moved</html>")).refresh_once()
    res = lib.resolve(UID)
    assert isinstance(res, Unavailable)
    assert res.code == "feed_error"


async def test_offline_is_not_an_error(db: Database) -> None:
    """SPEC v0.8 §8.2: without network no error, the figure says "loading"."""
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot())

    def offline(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    r = refresher(db, FeedServer(b""))
    r.http_factory = lambda: httpx.AsyncClient(transport=httpx.MockTransport(offline))
    await r.refresh_once()
    assert isinstance(lib.resolve(UID), Loading)
    feed = lib.podcasts.feed(podcast_snapshot().upserts.content[0].id)
    assert feed is None  # a different content id: nothing stored for it
    state = lib.podcasts.feed(lib.podcasts.targets()[0].content_id)
    assert state is not None
    assert (state.error, state.failures) == (None, 1)


async def test_no_eligible_episode(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot())
    await refresher(db, FeedServer(rss(item(1, 1, explicit="yes")))).refresh_once()
    res = lib.resolve(UID)
    assert isinstance(res, Unavailable)
    assert res.code == "no_episodes"


async def test_disabled_provider(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot())
    StateRepo(db).apply_device_config(DeviceConfig(providers_enabled=["local"]), 2)
    server = FeedServer(rss(item(1, 1)))
    await refresher(db, server).refresh_once()
    assert server.requests == []  # nothing fetched while switched off
    res = lib.resolve(UID)
    assert isinstance(res, Unavailable)
    assert res.code == "disabled"


async def test_broken_enclosures_end_in_feed_error_after_three_attempts(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot(keep_latest=1))
    server = FeedServer(rss(item(1, 1)))
    del server.files["/1.mp3"]
    r = refresher(db, server)
    for _ in range(3):
        assert isinstance(lib.resolve(UID), Loading)
        await r.refresh_once()
    res = lib.resolve(UID)
    assert isinstance(res, Unavailable)
    assert res.code == "feed_error"


async def test_interrupted_download_resumes_with_range(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot(keep_latest=1))
    server = FeedServer(rss(item(1, 1)))
    server.cut_after = 1000
    r = refresher(db, server)
    await r.refresh_once()
    assert isinstance(lib.resolve(UID), Loading)
    await r.refresh_once()
    assert server.requests[-1].headers["range"] == "bytes=1000-"
    plan = lib.resolve(UID)
    assert isinstance(plan, Playable)
    assert Path(plan.items[0].source).read_bytes() == server.files["/1.mp3"]


async def test_storage_full_is_reported_once(db: Database) -> None:
    from myboxi_agent.testing import FakeOutbox

    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot(keep_latest=1))
    outbox = FakeOutbox()
    r = refresher(db, FeedServer(rss(item(1, 1))), free=100, outbox=outbox)
    await r.refresh_once()
    await r.refresh_once()
    assert len(outbox.of("storage_full")) == 1
    assert isinstance(lib.resolve(UID), Loading)


async def test_loudness_is_measured_when_idle_and_used_for_playback(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot(keep_latest=1))
    playing = True
    loud = FakeLoudness(-21.34)
    r = refresher(db, FeedServer(rss(item(1, 1))), busy=lambda: playing, loudness=loud)
    await r.refresh_once()
    assert loud.measured == []  # SPEC v0.8 §8.2: preferably while nothing plays
    plan = lib.resolve(UID)
    assert isinstance(plan, Playable)
    assert plan.items[0].gain_db is None
    playing = False
    await r.refresh_once()
    plan = lib.resolve(UID)
    assert isinstance(plan, Playable)
    assert plan.items[0].gain_db == 5.3
    await r.refresh_once()
    assert len(loud.measured) == 1


async def test_podcast_rows_follow_the_library(db: Database) -> None:
    lib = LibraryRepo(db)
    snap = podcast_snapshot()
    lib.activate(snap)
    await refresher(db, FeedServer(rss(item(1, 1)))).refresh_once()
    assert db.conn.execute("SELECT count(*) FROM podcast_episode").fetchone()[0] == 1
    lib.activate(snap)  # activation replaces content rows: episodes survive
    assert db.conn.execute("SELECT count(*) FROM podcast_episode").fetchone()[0] == 1
    StateRepo(db).clear_tenant()  # unpaired: the server's podcasts are gone
    assert db.conn.execute("SELECT count(*) FROM podcast_episode").fetchone()[0] == 0


def test_resume_repo_keeps_the_item_key(db: Database) -> None:
    repo, token = ResumeRepo(db), uuid.uuid4()
    repo.save(token, ResumePoint(1, 5000, "k" * 32))
    assert repo.get(token) == ResumePoint(1, 5000, "k" * 32)


# --- resume by key (controller) ------------------------------------------------------------


def test_start_index_by_key() -> None:
    items = tuple(PlanItem(f"/{k}", k, 0, key=k) for k in ("new", "old"))
    plan = Playable(uuid.uuid4(), uuid.uuid4(), items, True, False, "off", "podcast")
    assert plan.start_index(ResumePoint(0, 10, "old")) == 1  # the list shifted
    assert plan.start_index(ResumePoint(1, 10, "gone")) is None
    assert plan.start_index(ResumePoint(1, 10)) == 1


# --- loudness ------------------------------------------------------------------------------


def test_integrated_loudness_from_the_summary() -> None:
    out = (
        "[ffmpeg] Parsed_ebur128_0: Summary:\n[ffmpeg]   Integrated loudness:\n"
        "[ffmpeg]     I:         -19.4 LUFS\n[ffmpeg]     Threshold: -29.6 LUFS\n"
    )
    assert integrated_loudness(out) == -19.4
    assert integrated_loudness("nothing") is None


@pytest.mark.parametrize(
    ("lufs", "gain"), [(-16.0, 0.0), (-19.44, 3.4), (-40.0, 12.0), (-2.0, -12.0), (-70.0, None)]
)
def test_gain_is_limited(lufs: float, gain: float | None) -> None:
    assert gain_for(lufs) == gain


@pytest.mark.skipif(shutil.which("mpv") is None, reason="mpv not installed")
async def test_real_mpv_measures_loudness(tmp_path: Path) -> None:
    import math
    import struct
    import wave

    path = tmp_path / "tone.wav"
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        amp = 0.1 * 32767  # -20 dBFS sine
        w.writeframes(
            b"".join(
                struct.pack("<h", int(amp * math.sin(2 * math.pi * 1000 * i / 16000)))
                for i in range(16000 * 3)
            )
        )
    lufs = await MpvLoudness().measure(path)
    assert lufs is not None
    assert -26.0 < lufs < -20.0  # a 1 kHz sine at -20 dBFS peak is about -23 LUFS (mono)


async def test_only_the_newest_hundred_episodes_are_stored(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(podcast_snapshot(keep_latest=1))
    feed = rss(*(item(n, 1 + n % 28) for n in range(1, 150)))
    server = FeedServer(feed)
    await refresher(db, server).refresh_once()
    assert db.conn.execute("SELECT count(*) FROM podcast_episode").fetchone()[0] == 100
