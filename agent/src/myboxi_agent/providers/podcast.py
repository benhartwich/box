"""Podcast refresh: feeds, episode downloads and loudness (SPEC v0.8 §8.2, §4.1).

Runs beside playback and never blocks it (CLAUDE.md rule 1): one feed or download at a time,
network errors only postpone the next attempt. Wakes every 15 minutes, after each sync and
when a podcast figure is still loading.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import hashlib
import logging
import math
import zlib
from collections.abc import Callable
from pathlib import Path, PurePosixPath
from typing import Protocol
from urllib.parse import urlsplit

import httpx

from myboxi_agent.adapters.loudness import gain_for
from myboxi_agent.adapters.outbox import EventOutbox
from myboxi_agent.core.clock import Clock
from myboxi_agent.providers.feed import MAX_FEED_BYTES, FeedError, FeedParser
from myboxi_agent.providers.netguard import BlockedAddress
from myboxi_agent.store.podcasts import (
    DOWNLOAD_ATTEMPTS,
    EpisodeRef,
    FeedState,
    FeedTarget,
    PodcastRepo,
)
from myboxi_agent.store.repos import AssetRepo, asset_path
from myboxi_protocol.events import StorageFullData

log = logging.getLogger(__name__)

REFRESH_S = 6 * 3600
JITTER_S = 30 * 60
RETRY_S = 15 * 60
WAKE_S = 15 * 60
MAX_EPISODE_BYTES = 500 * 1024 * 1024
DISK_RESERVE = 200 * 1024 * 1024
CHUNK = 256 * 1024
MB = 1024 * 1024

SUFFIXES = {
    "audio/mpeg": ".mp3", "audio/mp3": ".mp3", "audio/mp4": ".m4a", "audio/x-m4a": ".m4a",
    "audio/m4a": ".m4a", "audio/aac": ".aac", "audio/x-aac": ".aac", "audio/ogg": ".ogg",
    "application/ogg": ".ogg", "audio/opus": ".opus",
}  # fmt: skip
KNOWN_SUFFIXES = frozenset(SUFFIXES.values())


class Loudness(Protocol):
    async def measure(self, path: Path) -> float | None: ...


class DownloadFailed(Exception):
    pass


class StorageFull(Exception):
    pass


def episode_suffix(mime: str | None, url: str) -> str:
    """SPEC v0.8 §4: episodes keep their format in the asset store."""
    if mime in SUFFIXES:
        return SUFFIXES[mime]
    suffix = PurePosixPath(urlsplit(url).path).suffix.lower()
    return suffix if suffix in KNOWN_SUFFIXES else ".mp3"


def _error_code(exc: Exception) -> str | None:
    """Stored feed error; None means "offline", which is not an error (SPEC v0.8 §8.2)."""
    if isinstance(exc, FeedError):
        return "invalid"
    if isinstance(exc, httpx.TooManyRedirects):
        return "redirects"
    if isinstance(exc, httpx.UnsupportedProtocol | httpx.InvalidURL):
        return "url"
    if isinstance(exc.__cause__, BlockedAddress):
        return "blocked"
    return None


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


class PodcastRefresher:
    def __init__(
        self,
        *,
        repo: PodcastRepo,
        assets: AssetRepo,
        clock: Clock,
        http_factory: Callable[[], httpx.AsyncClient],
        outbox: EventOutbox | None,
        disk_free: Callable[[], int],
        enabled: Callable[[], bool],
        busy: Callable[[], bool],
        loudness: Loudness | None,
    ) -> None:
        self.repo = repo
        self.assets = assets
        self.clock = clock
        self.http_factory = http_factory
        self.outbox = outbox
        self.disk_free = disk_free
        self.enabled = enabled
        self.busy = busy
        self.loudness = loudness
        self._wake = asyncio.Event()
        self._storage_full_for: set[str] = set()

    def trigger(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while True:
            try:
                await self.refresh_once()
            except Exception:
                log.exception("podcast refresh failed")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._wake.wait(), WAKE_S)
            self._wake.clear()

    async def refresh_once(self) -> None:
        if not self.enabled():
            return
        targets = self.repo.targets()
        if targets:
            async with self.http_factory() as http:
                for target in targets:
                    state = self.repo.feed(target.content_id)
                    if state is None or self._due(target, state):
                        await self._fetch_feed(http, target, state)
                    else:
                        self.repo.reselect(target.content_id, target.keep_latest)
                await self._download_pending(http)
        await self._measure_pending()

    # --- feeds ------------------------------------------------------------------------------

    def _due(self, target: FeedTarget, state: FeedState) -> bool:
        if state.checked_at is None:
            return True
        now = self.clock.now()
        if state.checked_at > now:  # the clock went back (no RTC, SPEC §5.6)
            return True
        if state.failures:
            wait = min(RETRY_S * 2 ** (state.failures - 1), REFRESH_S)
        else:
            # Stable jitter per feed, so boxes do not all ask at the same second.
            wait = REFRESH_S + zlib.crc32(str(target.content_id).encode()) % JITTER_S
        return now - state.checked_at >= dt.timedelta(seconds=wait)

    async def _fetch_feed(
        self, http: httpx.AsyncClient, target: FeedTarget, state: FeedState | None
    ) -> None:
        headers: dict[str, str] = {}
        if state is not None and state.etag:
            headers["If-None-Match"] = state.etag
        if state is not None and state.last_modified:
            headers["If-Modified-Since"] = state.last_modified
        cid = target.content_id
        try:
            async with http.stream("GET", target.feed_url, headers=headers) as r:
                if r.status_code == 304:
                    self.repo.feed_unchanged(cid)
                    self.repo.reselect(cid, target.keep_latest)
                    return
                if r.status_code != 200:
                    self.repo.feed_failed(cid, f"http_{r.status_code}")
                    log.warning("feed failed", extra={"status": r.status_code})
                    return
                parser = FeedParser()
                size = 0
                async for chunk in r.aiter_bytes(CHUNK):
                    size += len(chunk)
                    if size > MAX_FEED_BYTES:
                        raise FeedError("feed too large")
                    parser.feed(chunk)
                feed = parser.close()
                etag, modified = r.headers.get("etag"), r.headers.get("last-modified")
        except (FeedError, httpx.HTTPError) as exc:
            code = _error_code(exc)
            self.repo.feed_failed(cid, code)
            log.warning("feed not read", extra={"code": code or "offline"})
            return
        self.repo.feed_ok(cid, feed, target.keep_latest, etag, modified)
        log.info("feed read", extra={"episodes": len(feed.episodes)})

    # --- downloads --------------------------------------------------------------------------

    async def _download_pending(self, http: httpx.AsyncClient) -> None:
        for episode in self.repo.selected():
            if episode.sha256 is not None and self.assets.has(episode.sha256):
                continue
            if episode.failures >= DOWNLOAD_ATTEMPTS:
                continue
            try:
                await self._download(http, episode)
            except StorageFull:
                return
            except (DownloadFailed, httpx.HTTPError) as exc:
                if _error_code(exc) is None and isinstance(exc, httpx.TransportError):
                    log.info("offline: episode download postponed")
                    return
                self.repo.download_failed(episode.content_id, episode.key)
                log.warning("episode download failed", extra={"error": str(exc)[:200]})

    def _ensure_space(self, episode: EpisodeRef, needed: int) -> None:
        free = self.disk_free() - DISK_RESERVE
        if needed > free:
            free += self.assets.evict(needed - free)
        if needed > free:
            if episode.key not in self._storage_full_for and self.outbox is not None:
                self._storage_full_for.add(episode.key)
                self.outbox.emit(
                    "storage_full",
                    StorageFullData(needed_mb=math.ceil(needed / MB), free_mb=max(free, 0) // MB),
                )
            raise StorageFull

    async def _download(self, http: httpx.AsyncClient, episode: EpisodeRef) -> None:
        tmp_dir = self.assets.db.asset_dir / "tmp"
        tmp_dir.mkdir(parents=True, exist_ok=True)
        part = tmp_dir / f"{episode.key}.part"
        offset = part.stat().st_size if part.exists() else 0
        if episode.length and episode.length > MAX_EPISODE_BYTES:
            raise DownloadFailed("episode too large")
        self._ensure_space(episode, max((episode.length or 0) - offset, 0))
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        async with http.stream("GET", episode.url, headers=headers) as r:
            if r.status_code == 200:
                offset = 0
            elif r.status_code == 416 and offset:
                part.unlink(missing_ok=True)
                raise DownloadFailed("range not satisfiable")
            elif r.status_code != 206:
                raise DownloadFailed(f"http_{r.status_code}")
            length = r.headers.get("content-length")
            total = offset + int(length) if length and length.isdigit() else None
            if total is not None:
                if total > MAX_EPISODE_BYTES:
                    raise DownloadFailed("episode too large")
                self._ensure_space(episode, total - offset)
            size = offset
            with part.open("ab" if offset else "wb") as fh:
                # As received (no re-chunking), so an interrupted download keeps its bytes.
                async for chunk in r.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_EPISODE_BYTES:
                        fh.close()
                        part.unlink(missing_ok=True)
                        raise DownloadFailed("episode too large")
                    fh.write(chunk)
        sha = await asyncio.to_thread(_sha256, part)
        if self.assets.has(sha):
            part.unlink(missing_ok=True)
        else:
            dest = asset_path(
                self.assets.db.asset_dir, sha, episode_suffix(episode.mime, episode.url)
            )
            dest.parent.mkdir(parents=True, exist_ok=True)
            part.replace(dest)
            self.assets.register(sha, size, dest)
        self.repo.downloaded(episode.content_id, episode.key, sha)
        log.info("episode downloaded", extra={"bytes": size})

    # --- loudness ---------------------------------------------------------------------------

    async def _measure_pending(self) -> None:
        if self.loudness is None:
            return
        for sha, path in self.repo.unmeasured():
            if self.busy():
                return  # SPEC v0.8 §8.2: preferably while nothing plays; next wake-up
            lufs = await self.loudness.measure(path) if path.is_file() else None
            gain = gain_for(lufs) if lufs is not None else None
            self.repo.measured(sha, gain)
            log.info("episode loudness", extra={"lufs": lufs, "gain_db": gain})
