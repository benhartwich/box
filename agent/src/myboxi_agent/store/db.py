"""Connection and schema migrations (``PRAGMA user_version``)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

MIGRATIONS: list[str] = [
    # 1: SPEC §4
    """
    CREATE TABLE sync_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        device_id TEXT NOT NULL,
        server_url TEXT,
        tenant_id TEXT,
        applied_config_rev INTEGER NOT NULL DEFAULT 0,
        applied_device_rev INTEGER NOT NULL DEFAULT 0,
        paired_at TEXT
    );
    CREATE TABLE secret (
        name TEXT PRIMARY KEY,
        value TEXT NOT NULL
    );
    CREATE TABLE token (
        id TEXT NOT NULL,
        uid TEXT NOT NULL,
        label TEXT NOT NULL,
        origin TEXT NOT NULL CHECK (origin IN ('server', 'local')),
        PRIMARY KEY (id),
        UNIQUE (uid, origin)
    );
    CREATE TABLE content (
        id TEXT PRIMARY KEY,
        kind TEXT NOT NULL,
        title TEXT NOT NULL,
        rev INTEGER NOT NULL,
        source TEXT NOT NULL DEFAULT '{}',
        origin TEXT NOT NULL CHECK (origin IN ('server', 'local'))
    );
    CREATE TABLE content_item (
        content_id TEXT NOT NULL REFERENCES content(id) ON DELETE CASCADE,
        position INTEGER NOT NULL,
        asset_sha256 TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        title TEXT NOT NULL,
        duration_ms INTEGER NOT NULL,
        PRIMARY KEY (content_id, position)
    );
    CREATE TABLE binding (
        token_id TEXT PRIMARY KEY REFERENCES token(id) ON DELETE CASCADE,
        content_id TEXT NOT NULL REFERENCES content(id) ON DELETE CASCADE,
        resume INTEGER NOT NULL,
        shuffle INTEGER NOT NULL,
        repeat TEXT NOT NULL,
        origin TEXT NOT NULL CHECK (origin IN ('server', 'local'))
    );
    CREATE TABLE device_config (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        config TEXT NOT NULL
    );
    CREATE TABLE resume_position (
        token_id TEXT PRIMARY KEY,
        item_index INTEGER NOT NULL,
        position_ms INTEGER NOT NULL,
        updated_at TEXT NOT NULL
    );
    CREATE TABLE local_asset (
        sha256 TEXT PRIMARY KEY,
        path TEXT NOT NULL,
        bytes INTEGER NOT NULL,
        verified_at TEXT NOT NULL,
        last_played_at TEXT
    );
    CREATE TABLE staged_change (
        config_rev INTEGER NOT NULL,
        device_rev INTEGER NOT NULL,
        snapshot TEXT NOT NULL,
        received_at TEXT NOT NULL,
        PRIMARY KEY (config_rev, device_rev)
    );
    CREATE TABLE outbox (
        id TEXT PRIMARY KEY,
        envelope TEXT NOT NULL,
        created_at TEXT NOT NULL
    );
    """,
    # 2: SPEC v0.8 §3.10, §4, §8.2 (podcasts). No foreign keys to ``content``: activating a
    # snapshot replaces the content rows (SPEC §5.2), episodes must survive that.
    """
    ALTER TABLE resume_position ADD COLUMN item_key TEXT;
    CREATE TABLE podcast_feed (
        content_id TEXT PRIMARY KEY,
        feed_url TEXT NOT NULL,
        etag TEXT,
        last_modified TEXT,
        checked_at TEXT,
        ok_at TEXT,
        error TEXT,
        failures INTEGER NOT NULL DEFAULT 0
    );
    CREATE TABLE podcast_episode (
        content_id TEXT NOT NULL,
        episode_key TEXT NOT NULL,
        title TEXT NOT NULL,
        url TEXT NOT NULL,
        mime TEXT,
        length INTEGER,
        published_at TEXT,
        feed_order INTEGER NOT NULL,
        duration_ms INTEGER NOT NULL DEFAULT 0,
        selected INTEGER NOT NULL DEFAULT 0,
        rank INTEGER,
        sha256 TEXT,
        gain_db REAL,
        measured INTEGER NOT NULL DEFAULT 0,
        failures INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (content_id, episode_key)
    );
    CREATE INDEX podcast_episode_sha ON podcast_episode (sha256);
    """,
    # 3: SPEC v0.11 §3.9: start points from the app, each applied once.
    """
    ALTER TABLE binding ADD COLUMN start_at TEXT;
    CREATE TABLE start_applied (
        token_id TEXT PRIMARY KEY,
        start_id TEXT NOT NULL
    );
    """,
    # 4: SPEC §6, §7.1 (M2): the broker account from pairing; the password lives in secret.
    """
    ALTER TABLE sync_state ADD COLUMN mqtt_host TEXT;
    ALTER TABLE sync_state ADD COLUMN mqtt_port INTEGER;
    ALTER TABLE sync_state ADD COLUMN mqtt_username TEXT;
    """,
    # 5: SPEC v0.13 §9.3: the own CA of a self-hosted server (PEM, not secret).
    """
    ALTER TABLE sync_state ADD COLUMN server_ca TEXT;
    """,
]


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, isolation_level=None, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = FULL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 5000")
    migrate(conn)
    path.chmod(0o600)  # holds the device secret (SPEC §4 ``secret``)
    return conn


def migrate(conn: sqlite3.Connection) -> None:
    version = int(conn.execute("PRAGMA user_version").fetchone()[0])
    for number, script in enumerate(MIGRATIONS[version:], start=version + 1):
        conn.execute("BEGIN IMMEDIATE")
        try:
            for statement in script.split(";"):
                if statement.strip():
                    conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {number}")
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise
