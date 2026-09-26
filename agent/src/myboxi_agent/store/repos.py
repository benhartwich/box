"""Repositories over the local database. Implements the core ports ``Library`` and ``ResumeStore``.

Only complete states are active: a snapshot whose assets are still downloading waits in
``staged_change`` (SPEC §5.3) while the previous binding keeps working.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import secrets
import sqlite3
import uuid
from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import ValidationError

from myboxi_agent.core.clock import Clock
from myboxi_agent.core.model import (
    Loading,
    PlanItem,
    Playable,
    Resolution,
    ResumePoint,
    Unavailable,
    Unknown,
)
from myboxi_agent.ids import uuid7
from myboxi_agent.store.podcasts import PodcastRepo
from myboxi_protocol.pairing import MqttCredentials
from myboxi_protocol.state import (
    DeviceConfig,
    PodcastSource,
    SpotifySource,
    StartAt,
    StateResponse,
    StreamSource,
)

Origin = Literal["server", "local"]


def asset_path(asset_dir: Path, sha256: str, suffix: str = ".opus") -> Path:
    """SPEC §4: ``assets/ab/cd/<sha256>.opus``; podcast episodes keep their format (v0.8)."""
    return asset_dir / sha256[:2] / sha256[2:4] / f"{sha256}{suffix}"


class Database:
    def __init__(self, conn: sqlite3.Connection, asset_dir: Path, clock: Clock) -> None:
        self.conn = conn
        self.asset_dir = asset_dir
        self.clock = clock
        # SPEC §3.3: the device id is generated once on the box and kept forever.
        with self.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO sync_state (id, device_id) VALUES (1, ?)", (str(uuid7()),)
            )

    @contextmanager
    def tx(self) -> Generator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        self.conn.execute("COMMIT")

    def now_iso(self) -> str:
        return self.clock.now().isoformat()


# --- identity, pairing, secrets -------------------------------------------------------------


@dataclass(frozen=True)
class SyncState:
    device_id: uuid.UUID
    server_url: str | None
    tenant_id: uuid.UUID | None
    applied_config_rev: int
    applied_device_rev: int
    paired_at: dt.datetime | None = None


def _write_mqtt(c: sqlite3.Connection, creds: MqttCredentials | None) -> None:
    c.execute(
        "UPDATE sync_state SET mqtt_host = ?, mqtt_port = ?, mqtt_username = ? WHERE id = 1",
        (creds.host, creds.port, creds.username) if creds else (None, None, None),
    )
    c.execute("DELETE FROM secret WHERE name = 'mqtt_password'")
    if creds is not None:
        c.execute("INSERT INTO secret (name, value) VALUES ('mqtt_password', ?)", (creds.password,))


class StateRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self) -> SyncState:
        row = self.db.conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()
        if row is None:
            with self.db.tx() as c:
                c.execute(
                    "INSERT OR IGNORE INTO sync_state (id, device_id) VALUES (1, ?)",
                    (str(uuid7()),),
                )
            row = self.db.conn.execute("SELECT * FROM sync_state WHERE id = 1").fetchone()
        return SyncState(
            device_id=uuid.UUID(row["device_id"]),
            server_url=row["server_url"],
            tenant_id=uuid.UUID(row["tenant_id"]) if row["tenant_id"] else None,
            applied_config_rev=row["applied_config_rev"],
            applied_device_rev=row["applied_device_rev"],
            paired_at=dt.datetime.fromisoformat(row["paired_at"]) if row["paired_at"] else None,
        )

    def set_server_url(self, url: str | None) -> None:
        """A different server means different credentials (SPEC §1.5): pair again."""
        current = self.get()
        if current.server_url == url:
            return
        if current.tenant_id is not None:
            self.clear_tenant()
        with self.db.tx() as c:
            c.execute("UPDATE sync_state SET server_url = ? WHERE id = 1", (url,))

    def pairing_key(self) -> str:
        """SPEC v0.12 §7.1: 256 random bits, created once with the device id and kept when
        unpairing; proves to the server that a pairing start comes from this box."""
        row = self.db.conn.execute("SELECT value FROM secret WHERE name = 'pairing_key'").fetchone()
        if row is not None:
            return str(row["value"])
        with self.db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO secret (name, value) VALUES ('pairing_key', ?)",
                (secrets.token_urlsafe(32),),
            )
        row = self.db.conn.execute("SELECT value FROM secret WHERE name = 'pairing_key'").fetchone()
        return str(row["value"])

    def set_paired(
        self, tenant_id: uuid.UUID, secret: str, mqtt: MqttCredentials | None = None
    ) -> None:
        """Tenant, device secret and broker account together: a power cut never leaves a
        paired box without its MQTT account (SPEC §7.1)."""
        with self.db.tx() as c:
            c.execute(
                "UPDATE sync_state SET tenant_id = ?, paired_at = ?, applied_config_rev = 0,"
                " applied_device_rev = 0 WHERE id = 1",
                (str(tenant_id), self.db.now_iso()),
            )
            c.execute(
                "INSERT INTO secret (name, value) VALUES ('device_secret', ?)"
                " ON CONFLICT (name) DO UPDATE SET value = excluded.value",
                (secret,),
            )
            _write_mqtt(c, mqtt)

    def device_secret(self) -> str | None:
        row = self.db.conn.execute(
            "SELECT value FROM secret WHERE name = 'device_secret'"
        ).fetchone()
        return row["value"] if row else None

    # SPEC §6, §7.1 (M2): the broker account from pairing; gone with the tenant.

    def set_mqtt(self, creds: MqttCredentials | None) -> None:
        with self.db.tx() as c:
            _write_mqtt(c, creds)

    def mqtt(self) -> MqttCredentials | None:
        row = self.db.conn.execute(
            "SELECT mqtt_host, mqtt_port, mqtt_username FROM sync_state WHERE id = 1"
        ).fetchone()
        secret = self.db.conn.execute(
            "SELECT value FROM secret WHERE name = 'mqtt_password'"
        ).fetchone()
        if row is None or secret is None or not row["mqtt_host"] or not row["mqtt_username"]:
            return None
        return MqttCredentials(
            host=row["mqtt_host"],
            port=row["mqtt_port"] or 8883,
            username=row["mqtt_username"],
            password=secret["value"],
        )

    # SPEC v0.9 §8.1: the Soloist key stays on the box, also after unpairing; never logged.

    def soloist_key(self) -> str | None:
        row = self.db.conn.execute(
            "SELECT value FROM secret WHERE name = 'soloist_api_key'"
        ).fetchone()
        return row["value"] if row else None

    def set_soloist_key(self, key: str | None) -> None:
        with self.db.tx() as c:
            if key is None:
                c.execute("DELETE FROM secret WHERE name = 'soloist_api_key'")
            else:
                c.execute(
                    "INSERT INTO secret (name, value) VALUES ('soloist_api_key', ?)"
                    " ON CONFLICT (name) DO UPDATE SET value = excluded.value",
                    (key,),
                )

    def clear_tenant(self) -> None:
        """Unpair (SPEC §7.3): drop credentials and the server's slice; keep local library."""
        with self.db.tx() as c:
            c.execute("DELETE FROM secret WHERE name IN ('device_secret', 'mqtt_password')")
            c.execute(
                "UPDATE sync_state SET tenant_id = NULL, paired_at = NULL,"
                " applied_config_rev = 0, applied_device_rev = 0,"
                " mqtt_host = NULL, mqtt_port = NULL, mqtt_username = NULL WHERE id = 1"
            )
            _delete_server_rows(c)
            c.execute("DELETE FROM device_config")
            c.execute("DELETE FROM staged_change")
            c.execute("DELETE FROM resume_position")
            _prune_podcasts(c)

    def device_config(self) -> DeviceConfig:
        row = self.db.conn.execute("SELECT config FROM device_config WHERE id = 1").fetchone()
        if row is None:
            return DeviceConfig()  # SPEC §3.4 defaults
        try:
            return DeviceConfig.model_validate_json(row["config"])
        except ValidationError:
            return DeviceConfig()

    def apply_device_config(self, config: DeviceConfig, device_rev: int) -> None:
        """Takes effect at once: limits must never wait for downloads."""
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO device_config (id, config) VALUES (1, ?)"
                " ON CONFLICT (id) DO UPDATE SET config = excluded.config",
                (config.model_dump_json(),),
            )
            c.execute("UPDATE sync_state SET applied_device_rev = ? WHERE id = 1", (device_rev,))


def _delete_server_rows(c: sqlite3.Connection) -> None:
    c.execute("DELETE FROM binding WHERE origin = 'server'")
    c.execute("DELETE FROM content WHERE origin = 'server'")
    c.execute("DELETE FROM token WHERE origin = 'server'")


def _prune_podcasts(c: sqlite3.Connection) -> None:
    """Feeds and episodes of podcasts that no longer exist (SPEC v0.8 §8.2)."""
    for table in ("podcast_feed", "podcast_episode"):
        c.execute(
            f"DELETE FROM {table} WHERE content_id NOT IN"  # noqa: S608 - fixed table names
            " (SELECT id FROM content WHERE kind = 'podcast')"
        )


# --- library ----------------------------------------------------------------------------------


class LibraryRepo:
    """``core.ports.Library`` plus snapshot activation (SPEC §5.3, §5.4)."""

    def __init__(self, db: Database) -> None:
        self.db = db
        self.podcasts = PodcastRepo(db)
        # SPEC v0.9 §8.1: why Spotify cannot play now (set by the app), None when ready.
        self.spotify_unavailable: Callable[[], str | None] = lambda: "not_configured"

    def resolve(self, uid: str) -> Resolution:
        c = self.db.conn
        rows = c.execute(
            "SELECT t.id AS token_id, b.content_id, b.resume, b.shuffle, b.repeat, b.start_at,"
            " ct.kind, ct.origin, ct.source"
            " FROM token t JOIN binding b ON b.token_id = t.id"
            " JOIN content ct ON ct.id = b.content_id"
            " WHERE t.uid = ? ORDER BY t.origin = 'server' DESC",
            (uid,),
        ).fetchall()
        if rows:
            resolution = self._plan(rows[0])
            if isinstance(resolution, Playable):
                return self._start_point(rows[0], resolution)
            return resolution
        staged = self._staged_token(uid)
        if staged is not None:
            return Loading(staged)
        return Unknown(uid)

    def _plan(self, row: sqlite3.Row) -> Resolution:
        token_id, content_id = uuid.UUID(row["token_id"]), uuid.UUID(row["content_id"])
        if row["kind"] == "podcast":
            return self._podcast(row, token_id, content_id)
        if row["kind"] == "spotify":
            return self._spotify(row, token_id, content_id)
        if row["kind"] == "stream":
            return self._stream(row, token_id, content_id)
        if row["kind"] != "collection":
            return Unavailable(
                token_id, content_id, provider=_provider(row["kind"]), code="not_available"
            )
        items = self.db.conn.execute(
            "SELECT asset_sha256, title, duration_ms FROM content_item"
            " WHERE content_id = ? ORDER BY position",
            (row["content_id"],),
        ).fetchall()
        if not items:
            return Unavailable(token_id, content_id, provider="local", code="empty")
        plan_items: list[PlanItem] = []
        for item in items:
            path = asset_path(self.db.asset_dir, item["asset_sha256"])
            if not path.is_file():
                return Unavailable(token_id, content_id, provider="local", code="asset_missing")
            plan_items.append(PlanItem(str(path), item["title"], item["duration_ms"]))
        return Playable(
            token_id=token_id,
            content_id=content_id,
            items=tuple(plan_items),
            resume=bool(row["resume"]),
            shuffle=bool(row["shuffle"]),
            repeat=row["repeat"],
        )

    def _start_point(self, row: sqlite3.Row, plan: Playable) -> Playable:
        """SPEC v0.11 §3.9: a new start point from the app applies once, at this placement."""
        if not row["start_at"] or plan.provider == "stream":
            return plan
        try:
            start = StartAt.model_validate_json(row["start_at"])
        except ValidationError:
            return plan
        applied = self.db.conn.execute(
            "SELECT start_id FROM start_applied WHERE token_id = ?", (row["token_id"],)
        ).fetchone()
        if applied is not None and applied["start_id"] == str(start.id):
            return plan
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO start_applied (token_id, start_id) VALUES (?, ?)"
                " ON CONFLICT (token_id) DO UPDATE SET start_id = excluded.start_id",
                (row["token_id"], str(start.id)),
            )
        index = start.item_index if plan.context or start.item_index < len(plan.items) else 0
        return dataclasses.replace(plan, start=ResumePoint(index, start.position_ms))

    def _podcast(self, row: sqlite3.Row, token_id: uuid.UUID, content_id: uuid.UUID) -> Resolution:
        """SPEC v0.8 §8.2, table "Figur auflegen"."""
        if "podcast" not in StateRepo(self.db).device_config().providers_enabled:
            return Unavailable(token_id, content_id, provider="podcast", code="disabled")
        try:
            source = PodcastSource.model_validate_json(row["source"])
        except ValidationError:
            return Unavailable(token_id, content_id, provider="podcast", code="feed_error")
        items = self.podcasts.plan_items(content_id, source.order)
        if items:
            return Playable(
                token_id=token_id,
                content_id=content_id,
                items=tuple(items),
                resume=bool(row["resume"]),
                shuffle=bool(row["shuffle"]),
                repeat=row["repeat"],
                provider="podcast",
            )
        match self.podcasts.status(content_id):
            case "loading":
                return Loading(token_id)
            case code:
                return Unavailable(token_id, content_id, provider="podcast", code=code)

    def _spotify(self, row: sqlite3.Row, token_id: uuid.UUID, content_id: uuid.UUID) -> Resolution:
        """SPEC v0.9 §8.1: the context URI, played and walked by Soloist."""
        if "spotify" not in StateRepo(self.db).device_config().providers_enabled:
            return Unavailable(token_id, content_id, provider="spotify", code="disabled")
        try:
            source = SpotifySource.model_validate_json(row["source"])
        except ValidationError:
            return Unavailable(token_id, content_id, provider="spotify", code="not_available")
        if code := self.spotify_unavailable():
            return Unavailable(token_id, content_id, provider="spotify", code=code)
        return Playable(
            token_id=token_id,
            content_id=content_id,
            items=(PlanItem(source.uri, source.uri, 0),),
            resume=bool(row["resume"]),
            shuffle=bool(row["shuffle"]),
            repeat=row["repeat"],
            provider="spotify",
            context=True,
        )

    def _stream(self, row: sqlite3.Row, token_id: uuid.UUID, content_id: uuid.UUID) -> Resolution:
        """SPEC v0.11 §8.3: live, no cache, no resume."""
        if "stream" not in StateRepo(self.db).device_config().providers_enabled:
            return Unavailable(token_id, content_id, provider="stream", code="disabled")
        try:
            source = StreamSource.model_validate_json(row["source"])
        except ValidationError:
            return Unavailable(token_id, content_id, provider="stream", code="not_available")
        return Playable(
            token_id=token_id,
            content_id=content_id,
            items=(PlanItem(source.url, source.url, 0),),
            resume=False,
            shuffle=False,
            repeat="off",
            provider="stream",
        )

    def _staged_token(self, uid: str) -> uuid.UUID | None:
        for row in self.db.conn.execute("SELECT snapshot FROM staged_change"):
            state = StateResponse.model_validate_json(row["snapshot"])
            bound = {b.token_id for b in state.upserts.binding}
            for token in state.upserts.token:
                if token.uid == uid and token.id in bound:
                    return token.id
        return None

    def uid_of(self, token_id: uuid.UUID) -> str | None:
        """For ``play_token`` from the app (SPEC §6.2)."""
        row = self.db.conn.execute(
            "SELECT uid FROM token WHERE id = ?", (str(token_id),)
        ).fetchone()
        return str(row["uid"]) if row else None

    def mark_played(self, sources: Sequence[str]) -> None:
        now = self.db.now_iso()
        with self.db.tx() as c:
            for source in sources:
                c.execute("UPDATE local_asset SET last_played_at = ? WHERE path = ?", (now, source))

    def stage(self, state: StateResponse) -> None:
        """Keep only the newest snapshot: a full snapshot supersedes older ones (SPEC §5.3)."""
        with self.db.tx() as c:
            c.execute("DELETE FROM staged_change")
            c.execute(
                "INSERT INTO staged_change (config_rev, device_rev, snapshot, received_at)"
                " VALUES (?, ?, ?, ?)",
                (state.config_rev, state.device_rev, state.model_dump_json(), self.db.now_iso()),
            )

    def staged(self) -> StateResponse | None:
        row = self.db.conn.execute(
            "SELECT snapshot FROM staged_change ORDER BY config_rev DESC LIMIT 1"
        ).fetchone()
        return StateResponse.model_validate_json(row["snapshot"]) if row else None

    def activate(self, state: StateResponse) -> None:
        """Replace the server slice atomically in one transaction (SPEC §5.2 step 5)."""
        with self.db.tx() as c:
            _delete_server_rows(c)
            for t in state.upserts.token:
                c.execute(
                    "INSERT INTO token (id, uid, label, origin) VALUES (?, ?, ?, 'server')",
                    (str(t.id), t.uid, t.label),
                )
            for ct in state.upserts.content:
                c.execute(
                    "INSERT INTO content (id, kind, title, rev, source, origin)"
                    " VALUES (?, ?, ?, ?, ?, 'server')",
                    (str(ct.id), ct.kind, ct.title, ct.rev, ct.source.model_dump_json()),
                )
            for i in state.upserts.content_item:
                c.execute(
                    "INSERT INTO content_item"
                    " (content_id, position, asset_sha256, bytes, title, duration_ms)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        str(i.content_id),
                        i.position,
                        i.asset_sha256,
                        i.bytes,
                        i.title,
                        i.duration_ms,
                    ),
                )
            for b in state.upserts.binding:
                c.execute(
                    "INSERT INTO binding"
                    " (token_id, content_id, resume, shuffle, repeat, start_at, origin)"
                    " VALUES (?, ?, ?, ?, ?, ?, 'server')",
                    (
                        str(b.token_id),
                        str(b.content_id),
                        b.resume,
                        b.shuffle,
                        b.repeat,
                        b.start_at.model_dump_json() if b.start_at else None,
                    ),
                )
            _prune_podcasts(c)
            c.execute(
                "UPDATE sync_state SET applied_config_rev = ? WHERE id = 1", (state.config_rev,)
            )
            c.execute("DELETE FROM staged_change WHERE config_rev <= ?", (state.config_rev,))

    def add_local(
        self, uid: str, label: str, title: str, assets: Sequence[tuple[str, int, str, int]]
    ) -> uuid.UUID:
        """Local library (SPEC v0.5 §4): assets are (sha256, bytes, title, duration_ms)."""
        token_id, content_id = uuid7(), uuid7()
        with self.db.tx() as c:
            old = c.execute(
                "SELECT id FROM token WHERE uid = ? AND origin = 'local'", (uid,)
            ).fetchone()
            if old is not None:
                c.execute(
                    "DELETE FROM content WHERE id IN"
                    " (SELECT content_id FROM binding WHERE token_id = ?)",
                    (old["id"],),
                )
                c.execute("DELETE FROM token WHERE id = ?", (old["id"],))
            c.execute(
                "INSERT INTO token (id, uid, label, origin) VALUES (?, ?, ?, 'local')",
                (str(token_id), uid, label),
            )
            c.execute(
                "INSERT INTO content (id, kind, title, rev, source, origin)"
                " VALUES (?, 'collection', ?, 1, '{}', 'local')",
                (str(content_id), title),
            )
            for pos, (sha, size, item_title, duration) in enumerate(assets):
                c.execute(
                    "INSERT INTO content_item VALUES (?, ?, ?, ?, ?, ?)",
                    (str(content_id), pos, sha, size, item_title, duration),
                )
            c.execute(
                "INSERT INTO binding (token_id, content_id, resume, shuffle, repeat, origin)"
                " VALUES (?, ?, 1, 0, 'off', 'local')",
                (str(token_id), str(content_id)),
            )
        return token_id


def _provider(kind: str) -> str:
    return {"podcast": "podcast", "spotify": "spotify", "stream": "stream"}.get(kind, "local")


# --- resume -----------------------------------------------------------------------------------


class ResumeRepo:
    """``core.ports.ResumeStore``. Committed at once: CLAUDE.md rule 5."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def get(self, token_id: uuid.UUID) -> ResumePoint | None:
        row = self.db.conn.execute(
            "SELECT item_index, position_ms, item_key FROM resume_position WHERE token_id = ?",
            (str(token_id),),
        ).fetchone()
        return ResumePoint(row["item_index"], row["position_ms"], row["item_key"]) if row else None

    def save(self, token_id: uuid.UUID, point: ResumePoint) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO resume_position"
                " (token_id, item_index, position_ms, item_key, updated_at)"
                " VALUES (?, ?, ?, ?, ?) ON CONFLICT (token_id) DO UPDATE SET"
                " item_index = excluded.item_index, position_ms = excluded.position_ms,"
                " item_key = excluded.item_key, updated_at = excluded.updated_at",
                (
                    str(token_id),
                    point.item_index,
                    point.position_ms,
                    point.item_key,
                    self.db.now_iso(),
                ),
            )


# --- assets -----------------------------------------------------------------------------------


class AssetRepo:
    def __init__(self, db: Database) -> None:
        self.db = db

    def path(self, sha256: str) -> Path:
        return asset_path(self.db.asset_dir, sha256)

    def stored_path(self, sha256: str) -> Path | None:
        row = self.db.conn.execute(
            "SELECT path FROM local_asset WHERE sha256 = ?", (sha256,)
        ).fetchone()
        return Path(row["path"]) if row is not None else None

    def has(self, sha256: str) -> bool:
        path = self.stored_path(sha256)
        return path is not None and path.is_file()

    def register(self, sha256: str, size: int, path: Path | None = None) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT INTO local_asset (sha256, path, bytes, verified_at) VALUES (?, ?, ?, ?)"
                " ON CONFLICT (sha256) DO UPDATE SET verified_at = excluded.verified_at,"
                " path = excluded.path, bytes = excluded.bytes",
                (sha256, str(path or self.path(sha256)), size, self.db.now_iso()),
            )

    def referenced(self) -> set[str]:
        """Assets reachable from the active or staged state, and the selected podcast
        episodes; never evicted (SPEC §4.1, v0.8 §8.2)."""
        shas = {
            r["asset_sha256"] for r in self.db.conn.execute("SELECT asset_sha256 FROM content_item")
        }
        for row in self.db.conn.execute("SELECT snapshot FROM staged_change"):
            state = StateResponse.model_validate_json(row["snapshot"])
            shas |= {i.asset_sha256 for i in state.upserts.content_item}
        return shas | PodcastRepo(self.db).referenced()

    def evictable(self) -> list[tuple[str, int]]:
        """Unreferenced assets, least recently played first (SPEC §4.1)."""
        keep = self.referenced()
        rows = self.db.conn.execute(
            "SELECT sha256, bytes FROM local_asset"
            " ORDER BY last_played_at IS NOT NULL, COALESCE(last_played_at, verified_at)"
        ).fetchall()
        return [(r["sha256"], r["bytes"]) for r in rows if r["sha256"] not in keep]

    def evict(self, needed: int) -> int:
        """Delete unreferenced assets, least recently played first, until ``needed`` bytes
        are free (SPEC §4.1). Returns the bytes freed."""
        freed = 0
        for sha, size in self.evictable():
            if freed >= needed:
                break
            self.delete(sha)
            freed += size
        return freed

    def delete(self, sha256: str) -> None:
        stored = self.stored_path(sha256)
        if stored is not None:
            stored.unlink(missing_ok=True)
        self.path(sha256).unlink(missing_ok=True)
        with self.db.tx() as c:
            c.execute("DELETE FROM local_asset WHERE sha256 = ?", (sha256,))


# --- outbox -----------------------------------------------------------------------------------


class OutboxRepo:
    """Events wait here until the server confirmed them (SPEC §6.5, §7.3)."""

    def __init__(self, db: Database) -> None:
        self.db = db

    def add(self, event_id: str, envelope: dict[str, Any]) -> None:
        with self.db.tx() as c:
            c.execute(
                "INSERT OR IGNORE INTO outbox (id, envelope, created_at) VALUES (?, ?, ?)",
                (event_id, json.dumps(envelope), self.db.now_iso()),
            )

    def pending(self, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.db.conn.execute(
            "SELECT envelope FROM outbox ORDER BY id LIMIT ?", (limit,)
        ).fetchall()
        return [json.loads(r["envelope"]) for r in rows]

    def remove(self, ids: Sequence[str]) -> None:
        with self.db.tx() as c:
            c.executemany("DELETE FROM outbox WHERE id = ?", [(i,) for i in ids])

    def count(self) -> int:
        return int(self.db.conn.execute("SELECT count(*) FROM outbox").fetchone()[0])
