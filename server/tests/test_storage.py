"""Filesystem asset store (SPEC §3.8)."""

from __future__ import annotations

import os
import stat
from pathlib import Path

import pytest

from myboxi_server.storage.base import relpath_for
from myboxi_server.storage.filesystem import FilesystemAssetStore

SHA = "ab" * 32


def test_relpath_layout() -> None:
    assert relpath_for(SHA, "opus") == f"ab/ab/{SHA}.opus"
    with pytest.raises(ValueError, match="sha256"):
        relpath_for("../etc", "opus")
    with pytest.raises(ValueError, match="extension"):
        relpath_for(SHA, "op/us")


def test_path_cannot_escape_root(tmp_path: Path) -> None:
    store = FilesystemAssetStore(tmp_path / "assets")
    with pytest.raises(ValueError, match="escapes"):
        store.path("../outside")


async def test_put_is_idempotent_and_inherits_directory_group(tmp_path: Path) -> None:
    """nginx reads assets through the directory's group (setgid), see docs/betrieb-debian13.md."""
    root = tmp_path / "assets"
    root.mkdir()
    if os.geteuid() == 0:
        os.chown(root, -1, 12345)
    root.chmod(0o2750)
    store = FilesystemAssetStore(root)
    src = tmp_path / "tmp.opus"
    src.write_bytes(b"data")
    rel = relpath_for(SHA, "opus")
    await store.put(src, rel)
    dest = store.path(rel)
    assert dest.read_bytes() == b"data"
    assert not src.exists()
    assert stat.S_IMODE(dest.stat().st_mode) == 0o640
    assert dest.stat().st_gid == root.stat().st_gid
    assert not list(dest.parent.glob(".*.part"))
    # Second put of the same content: no change, source removed.
    src.write_bytes(b"data")
    await store.put(src, rel)
    assert not src.exists()
    assert await store.exists(rel)
    await store.delete(rel)
    assert not await store.exists(rel)
