"""Local store (SPEC §4, §4.1, §5.3; CLAUDE.md rule 5)."""

from __future__ import annotations

import os
import sqlite3
import stat
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest

from myboxi_agent.core.clock import FakeClock
from myboxi_agent.core.model import Loading, Playable, ResumePoint, Unavailable, Unknown
from myboxi_agent.store.db import MIGRATIONS, connect
from myboxi_agent.store.repos import (
    AssetRepo,
    Database,
    LibraryRepo,
    OutboxRepo,
    ResumeRepo,
    StateRepo,
    asset_path,
)
from myboxi_protocol.state import DeviceConfig, StateResponse

UID = "04A2B3C4D5E680"
SHA_A, SHA_B = "a" * 64, "b" * 64


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(connect(tmp_path / "myboxi.db"), tmp_path / "assets", FakeClock())


def snapshot(
    *,
    kind: str = "collection",
    shas: tuple[str, ...] = (SHA_A, SHA_B),
    config_rev: int = 3,
    token_id: uuid.UUID | None = None,
    uid: str = UID,
) -> StateResponse:
    token_id = token_id or uuid.uuid4()
    content_id = uuid.uuid4()
    source: dict[str, Any] = {
        "collection": {},
        "podcast": {"feed_url": "https://example.org/f.xml"},
        "spotify": {"uri": "spotify:album:4aawyAB9vmqN3uQ7FjRGTy"},
    }[kind]
    return StateResponse.model_validate(
        {
            "full": True,
            "config_rev": config_rev,
            "device_rev": 1,
            "upserts": {
                "token": [{"id": str(token_id), "uid": uid, "label": "Bibi"}],
                "content": [
                    {"id": str(content_id), "kind": kind, "title": "F", "rev": 1, "source": source}
                ],
                "content_item": [
                    {
                        "content_id": str(content_id), "position": i, "asset_sha256": sha,
                        "bytes": 10, "title": f"T{i}", "duration_ms": 1000,
                    }
                    for i, sha in enumerate(shas)
                ],
                "binding": [{"token_id": str(token_id), "content_id": str(content_id)}],
            },
            "device_config": {"max_volume": 40},
        }
    )  # fmt: skip


def put_asset(db: Database, sha: str) -> None:
    path = asset_path(db.asset_dir, sha)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x" * 10)
    AssetRepo(db).register(sha, 10)


def test_schema_wal_and_permissions(tmp_path: Path) -> None:
    conn = connect(tmp_path / "myboxi.db")
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2  # FULL
    assert conn.execute("PRAGMA user_version").fetchone()[0] == len(MIGRATIONS)
    assert stat.S_IMODE((tmp_path / "myboxi.db").stat().st_mode) == 0o600
    connect(tmp_path / "myboxi.db")  # idempotent


def test_device_id_is_generated_once(tmp_path: Path) -> None:
    first = StateRepo(Database(connect(tmp_path / "m.db"), tmp_path, FakeClock())).get().device_id
    second = StateRepo(Database(connect(tmp_path / "m.db"), tmp_path, FakeClock())).get().device_id
    assert first == second
    assert first.version == 7


def test_resolve_unknown_playable_and_missing_asset(db: Database) -> None:
    lib = LibraryRepo(db)
    assert lib.resolve(UID) == Unknown(UID)
    lib.activate(snapshot())
    assert isinstance(lib.resolve(UID), Unavailable)  # assets not on disk
    put_asset(db, SHA_A)
    put_asset(db, SHA_B)
    plan = lib.resolve(UID)
    assert isinstance(plan, Playable)
    assert [i.title for i in plan.items] == ["T0", "T1"]
    assert plan.items[0].source == str(asset_path(db.asset_dir, SHA_A))


def test_spotify_not_on_the_box_yet_is_unavailable(db: Database) -> None:
    LibraryRepo(db).activate(snapshot(kind="spotify", shas=()))
    res = LibraryRepo(db).resolve(UID)
    assert isinstance(res, Unavailable)
    assert res.provider == "spotify"


def test_migration_2_keeps_resume_positions(tmp_path: Path) -> None:
    """SPEC v0.8: a box updated from v0.7 keeps its data; old points have no item key."""
    path = tmp_path / "myboxi.db"
    conn = sqlite3.connect(path, isolation_level=None)
    for statement in MIGRATIONS[0].split(";"):
        if statement.strip():
            conn.execute(statement)
    conn.execute("PRAGMA user_version = 1")
    conn.execute("INSERT INTO resume_position VALUES ('t', 2, 5000, '2026-09-24T12:00:00+00:00')")
    conn.close()
    db = Database(connect(path), tmp_path / "assets", FakeClock())
    row = db.conn.execute("SELECT * FROM resume_position").fetchone()
    assert (row["item_index"], row["position_ms"], row["item_key"]) == (2, 5000, None)


def test_staged_binding_is_loading_and_old_binding_stays_active(db: Database) -> None:
    """SPEC §5.3."""
    lib = LibraryRepo(db)
    new = snapshot(uid="04FFFFFFFF", config_rev=5)
    lib.stage(new)
    assert isinstance(lib.resolve("04FFFFFFFF"), Loading)
    put_asset(db, SHA_A)
    put_asset(db, SHA_B)
    old = snapshot(config_rev=3)
    lib.activate(old)
    lib.stage(new)
    assert isinstance(lib.resolve(UID), Playable)  # still active while the new one loads
    lib.activate(new)
    assert isinstance(lib.resolve(UID), Unknown)
    assert isinstance(lib.resolve("04FFFFFFFF"), Playable)
    assert lib.staged() is None
    assert StateRepo(db).get().applied_config_rev == 5


def test_activation_is_atomic(db: Database) -> None:
    lib = LibraryRepo(db)
    put_asset(db, SHA_A)
    put_asset(db, SHA_B)
    lib.activate(snapshot())
    broken = snapshot(config_rev=9)
    broken.upserts.token.append(broken.upserts.token[0])  # duplicate id → integrity error
    with pytest.raises(sqlite3.IntegrityError):
        lib.activate(broken)
    assert isinstance(lib.resolve(UID), Playable)
    assert StateRepo(db).get().applied_config_rev == 3


def test_server_binding_wins_over_local(db: Database) -> None:
    lib = LibraryRepo(db)
    put_asset(db, SHA_A)
    lib.add_local(UID, "Lokal", "Lokale Sammlung", [(SHA_A, 10, "Lokal 1", 0)])
    local = lib.resolve(UID)
    assert isinstance(local, Playable)
    assert [i.title for i in local.items] == ["Lokal 1"]
    put_asset(db, SHA_B)
    lib.activate(snapshot())
    server = lib.resolve(UID)
    assert isinstance(server, Playable)
    assert [i.title for i in server.items] == ["T0", "T1"]
    StateRepo(db).clear_tenant()  # unpair keeps the local library
    back = lib.resolve(UID)
    assert isinstance(back, Playable)
    assert [i.title for i in back.items] == ["Lokal 1"]


def test_device_config_applies_immediately(db: Database) -> None:
    repo = StateRepo(db)
    assert repo.device_config() == DeviceConfig()
    repo.apply_device_config(DeviceConfig(max_volume=40), device_rev=4)
    assert repo.device_config().max_volume == 40
    assert repo.get().applied_device_rev == 4


def test_pairing_credentials_and_unpair(db: Database) -> None:
    repo = StateRepo(db)
    tenant = uuid.uuid4()
    repo.set_paired(tenant, "s" * 43)
    assert repo.get().tenant_id == tenant
    assert repo.device_secret() == "s" * 43
    repo.clear_tenant()
    assert repo.device_secret() is None
    assert repo.get().tenant_id is None


def test_resume_survives_a_hard_crash(tmp_path: Path) -> None:
    """CLAUDE.md rule 5: a committed position is on disk even if the process dies at once."""
    token = uuid.uuid4()
    code = f"""
import os, uuid
from pathlib import Path
from myboxi_agent.core.clock import FakeClock
from myboxi_agent.core.model import ResumePoint
from myboxi_agent.store.db import connect
from myboxi_agent.store.repos import Database, ResumeRepo
db = Database(connect(Path({str(tmp_path / "m.db")!r})), Path("."), FakeClock())
ResumeRepo(db).save(uuid.UUID({str(token)!r}), ResumePoint(2, 12345))
os._exit(1)
"""
    result = subprocess.run([sys.executable, "-c", code], check=False, env=os.environ.copy())
    assert result.returncode == 1
    db = Database(connect(tmp_path / "m.db"), tmp_path, FakeClock())
    assert ResumeRepo(db).get(token) == ResumePoint(2, 12345)


def test_outbox_dedupes_and_removes(db: Database) -> None:
    box = OutboxRepo(db)
    box.add("01A", {"id": "01A"})
    box.add("01A", {"id": "01A"})
    box.add("01B", {"id": "01B"})
    assert [e["id"] for e in box.pending()] == ["01A", "01B"]
    box.remove(["01A"])
    assert box.count() == 1


def test_eviction_keeps_referenced_and_staged_assets(db: Database) -> None:
    """SPEC §4.1: bound assets are never evicted; LRU for the rest."""
    lib, assets = LibraryRepo(db), AssetRepo(db)
    for sha in (SHA_A, SHA_B, "c" * 64, "d" * 64):
        put_asset(db, sha)
    lib.activate(snapshot(shas=(SHA_A,)))
    lib.stage(snapshot(shas=(SHA_B,), config_rev=7))
    lib.mark_played([str(assets.path("c" * 64))])
    assert [s for s, _ in assets.evictable()] == ["d" * 64, "c" * 64]
    assets.delete("d" * 64)
    assert not assets.has("d" * 64)
    assert not asset_path(db.asset_dir, "d" * 64).exists()


def test_resume_repo_roundtrip(db: Database) -> None:
    repo = ResumeRepo(db)
    token = uuid.uuid4()
    assert repo.get(token) is None
    repo.save(token, ResumePoint(1, 5))
    repo.save(token, ResumePoint(2, 6))
    assert repo.get(token) == ResumePoint(2, 6)
