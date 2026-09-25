"""Podcast feeds and episodes on the box (SPEC v0.8 §4, §8.2).

Only eligible episodes are stored (audio, not explicit), ranked newest first. The newest
``keep_latest`` are ``selected``: they are downloaded, played and never evicted.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from pydantic import ValidationError

from myboxi_agent.core.model import PlanItem
from myboxi_agent.providers.feed import Feed
from myboxi_protocol.state import PodcastSource

if TYPE_CHECKING:
    from myboxi_agent.store.repos import Database

Order = Literal["newest_first", "oldest_first"]
DOWNLOAD_ATTEMPTS = 3  # then the episode counts as broken (``feed_error`` if all are)
MAX_STORED = 100  # the largest keep_latest (SPEC §3.6); older episodes are not kept


@dataclass(frozen=True)
class FeedTarget:
    content_id: uuid.UUID
    feed_url: str
    keep_latest: int
    order: Order


@dataclass(frozen=True)
class FeedState:
    feed_url: str
    etag: str | None
    last_modified: str | None
    checked_at: dt.datetime | None
    ok_at: dt.datetime | None
    error: str | None
    failures: int


@dataclass(frozen=True)
class EpisodeRef:
    content_id: uuid.UUID
    key: str
    title: str
    url: str
    mime: str | None
    length: int | None
    sha256: str | None
    failures: int


PodcastStatus = Literal["loading", "feed_error", "no_episodes"]


def _time(value: str | None) -> dt.datetime | None:
    return dt.datetime.fromisoformat(value) if value else None


class PodcastRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    # --- feeds ------------------------------------------------------------------------------

    def targets(self) -> list[FeedTarget]:
        """Bound podcasts of the active library. Feed rows follow: new ones are added, a
        changed URL starts over, feeds no longer bound disappear with their episodes."""
        rows = self.db.conn.execute(
            "SELECT c.id, c.source FROM content c WHERE c.kind = 'podcast'"
            " AND EXISTS (SELECT 1 FROM binding b WHERE b.content_id = c.id) ORDER BY c.id"
        ).fetchall()
        targets: list[FeedTarget] = []
        for row in rows:
            try:
                source = PodcastSource.model_validate_json(row["source"])
            except ValidationError:
                continue
            targets.append(
                FeedTarget(uuid.UUID(row["id"]), source.feed_url, source.keep_latest, source.order)
            )
        with self.db.tx() as c:
            known = {
                r["content_id"]: r["feed_url"]
                for r in c.execute("SELECT content_id, feed_url FROM podcast_feed")
            }
            wanted = {str(t.content_id): t for t in targets}
            for cid in known.keys() - wanted.keys():
                _drop(c, cid)
            for cid, target in wanted.items():
                if known.get(cid) == target.feed_url:
                    continue
                _drop(c, cid)
                c.execute(
                    "INSERT INTO podcast_feed (content_id, feed_url) VALUES (?, ?)",
                    (cid, target.feed_url),
                )
        return targets

    def feed(self, content_id: uuid.UUID) -> FeedState | None:
        row = self.db.conn.execute(
            "SELECT * FROM podcast_feed WHERE content_id = ?", (str(content_id),)
        ).fetchone()
        if row is None:
            return None
        return FeedState(
            feed_url=row["feed_url"],
            etag=row["etag"],
            last_modified=row["last_modified"],
            checked_at=_time(row["checked_at"]),
            ok_at=_time(row["ok_at"]),
            error=row["error"],
            failures=row["failures"],
        )

    def feed_ok(  # a fresh feed also gives broken downloads a fresh chance
        self,
        content_id: uuid.UUID,
        feed: Feed,
        keep_latest: int,
        etag: str | None,
        last_modified: str | None,
    ) -> None:
        cid, now = str(content_id), self.db.now_iso()
        episodes = feed.eligible()[:MAX_STORED]
        with self.db.tx() as c:
            keys = [e.key for e in episodes]
            placeholders = ",".join("?" * len(keys))
            c.execute(
                f"DELETE FROM podcast_episode WHERE content_id = ?"  # noqa: S608 - placeholders only
                f" AND episode_key NOT IN ({placeholders})",
                (cid, *keys),
            )
            for rank, e in enumerate(episodes):
                c.execute(
                    "INSERT INTO podcast_episode (content_id, episode_key, title, url, mime,"
                    " length, published_at, feed_order, duration_ms, selected, rank)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                    " ON CONFLICT (content_id, episode_key) DO UPDATE SET"
                    " title = excluded.title, url = excluded.url, mime = excluded.mime,"
                    " length = excluded.length, published_at = excluded.published_at,"
                    " feed_order = excluded.feed_order, duration_ms = excluded.duration_ms,"
                    " selected = excluded.selected, rank = excluded.rank, failures = 0",
                    (
                        cid, e.key, e.title, e.url, e.mime, e.length,
                        e.published_at.isoformat() if e.published_at else None,
                        e.feed_order, e.duration_ms, int(rank < keep_latest), rank,
                    ),
                )  # fmt: skip
            c.execute(
                "UPDATE podcast_feed SET etag = ?, last_modified = ?, checked_at = ?,"
                " ok_at = ?, error = NULL, failures = 0 WHERE content_id = ?",
                (etag, last_modified, now, now, cid),
            )

    def reselect(self, content_id: uuid.UUID, keep_latest: int) -> None:
        """``keep_latest`` changed without a new feed (e.g. 304)."""
        with self.db.tx() as c:
            c.execute(
                "UPDATE podcast_episode SET selected = (rank < ?) WHERE content_id = ?",
                (keep_latest, str(content_id)),
            )

    def feed_unchanged(self, content_id: uuid.UUID) -> None:
        now = self.db.now_iso()
        with self.db.tx() as c:
            c.execute(
                "UPDATE podcast_feed SET checked_at = ?, ok_at = ?, error = NULL, failures = 0"
                " WHERE content_id = ?",
                (now, now, str(content_id)),
            )

    def feed_failed(self, content_id: uuid.UUID, code: str | None) -> None:
        """``code`` None: network down, nothing to report (SPEC v0.8 §8.2), only retry."""
        with self.db.tx() as c:
            c.execute(
                "UPDATE podcast_feed SET checked_at = ?, failures = failures + 1,"
                " error = COALESCE(?, error) WHERE content_id = ?",
                (self.db.now_iso(), code, str(content_id)),
            )

    # --- episodes ---------------------------------------------------------------------------

    def _refs(self, where: str, args: tuple[object, ...] = ()) -> list[EpisodeRef]:
        rows = self.db.conn.execute(
            "SELECT content_id, episode_key, title, url, mime, length, sha256, failures"  # noqa: S608
            f" FROM podcast_episode WHERE {where} ORDER BY content_id, rank",
            args,
        ).fetchall()
        return [
            EpisodeRef(
                uuid.UUID(r["content_id"]), r["episode_key"], r["title"], r["url"], r["mime"],
                r["length"], r["sha256"], r["failures"],
            )
            for r in rows
        ]  # fmt: skip

    def selected(self) -> list[EpisodeRef]:
        return self._refs("selected = 1")

    def downloaded(self, content_id: uuid.UUID, key: str, sha256: str) -> None:
        """A new file needs a new loudness measurement; the same file keeps its value."""
        with self.db.tx() as c:
            c.execute(
                "UPDATE podcast_episode SET"
                " measured = CASE WHEN sha256 IS ? THEN measured ELSE 0 END,"
                " gain_db = CASE WHEN sha256 IS ? THEN gain_db ELSE NULL END,"
                " sha256 = ?, failures = 0 WHERE content_id = ? AND episode_key = ?",
                (sha256, sha256, sha256, str(content_id), key),
            )

    def download_failed(self, content_id: uuid.UUID, key: str) -> None:
        with self.db.tx() as c:
            c.execute(
                "UPDATE podcast_episode SET failures = failures + 1"
                " WHERE content_id = ? AND episode_key = ?",
                (str(content_id), key),
            )

    def unmeasured(self) -> list[tuple[str, Path]]:
        """Downloaded selected episodes without a loudness value: (sha256, path)."""
        rows = self.db.conn.execute(
            "SELECT DISTINCT e.sha256, a.path FROM podcast_episode e"
            " JOIN local_asset a ON a.sha256 = e.sha256"
            " WHERE e.selected = 1 AND e.measured = 0 ORDER BY e.rank"
        ).fetchall()
        return [(r["sha256"], Path(r["path"])) for r in rows]

    def measured(self, sha256: str, gain_db: float | None) -> None:
        with self.db.tx() as c:
            c.execute(
                "UPDATE podcast_episode SET gain_db = ?, measured = 1 WHERE sha256 = ?",
                (gain_db, sha256),
            )

    def referenced(self) -> set[str]:
        """Selected episodes count as bound (SPEC v0.8 §4.1)."""
        return {
            r["sha256"]
            for r in self.db.conn.execute(
                "SELECT sha256 FROM podcast_episode WHERE selected = 1 AND sha256 IS NOT NULL"
            )
        }

    # --- playback ---------------------------------------------------------------------------

    def plan_items(self, content_id: uuid.UUID, order: Order) -> list[PlanItem]:
        direction = "ASC" if order == "newest_first" else "DESC"
        rows = self.db.conn.execute(
            "SELECT e.episode_key, e.title, e.duration_ms, e.gain_db, a.path"  # noqa: S608
            " FROM podcast_episode e JOIN local_asset a ON a.sha256 = e.sha256"
            f" WHERE e.content_id = ? AND e.selected = 1 ORDER BY e.rank {direction}",
            (str(content_id),),
        ).fetchall()
        return [
            PlanItem(
                source=r["path"],
                title=r["title"],
                duration_ms=r["duration_ms"],
                key=r["episode_key"],
                gain_db=r["gain_db"],
            )
            for r in rows
            if Path(r["path"]).is_file()
        ]

    def status(self, content_id: uuid.UUID) -> PodcastStatus:
        """Why nothing can play yet (SPEC v0.8 §8.2, table "Figur auflegen")."""
        feed = self.feed(content_id)
        if feed is None:
            return "loading"
        if feed.ok_at is None:
            return "feed_error" if feed.error else "loading"
        selected = self._refs("content_id = ? AND selected = 1", (str(content_id),))
        if not selected:
            return "no_episodes"
        if all(e.failures >= DOWNLOAD_ATTEMPTS for e in selected):
            return "feed_error"
        return "loading"


def _drop(c: sqlite3.Connection, content_id: str) -> None:
    c.execute("DELETE FROM podcast_feed WHERE content_id = ?", (content_id,))
    c.execute("DELETE FROM podcast_episode WHERE content_id = ?", (content_id,))
