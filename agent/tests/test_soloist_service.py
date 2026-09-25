"""Soloist install, update and supervision (SPEC v0.9 §8.1, §6.4). No Soloist binary here:
the archives are built in the test (CLAUDE.md: never Soloist in repo, image or fixtures)."""

from __future__ import annotations

import datetime as dt
import io
import tarfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from myboxi_agent.adapters.soloist_service import UNIT, UPDATE_UNIT, SoloistService
from myboxi_agent.core.clock import FakeClock
from myboxi_agent.core.model import Playable, Unavailable
from myboxi_agent.soloist import runner
from myboxi_agent.soloist.install import (
    Installer,
    SoloistPaths,
    SoloistState,
    build_time,
    read_state,
    soloist_client,
    write_state,
)
from myboxi_agent.store.db import connect
from myboxi_agent.store.repos import Database, LibraryRepo, StateRepo
from myboxi_protocol.state import DeviceConfig, StateResponse

NOW = dt.datetime(2026, 9, 25, 20, 0, tzinfo=dt.UTC)
MODIFIED = "Fri, 25 Sep 2026 18:13:22 GMT"
URI = "spotify:album:4aawyAB9vmqN3uQ7FjRGTy"


def elf(machine: int = 62, elf_class: int = 2) -> bytes:
    return (
        b"\x7fELF"
        + bytes([elf_class, 1, 1])
        + bytes(9)  # e_ident ends at 16
        + b"\x02\x00"  # e_type
        + machine.to_bytes(2, "little")
        + bytes(100)
    )


def archive(binary: bytes | None = None, name: str = "soloist") -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for member, data in ((name, binary or elf()), ("CHANGELOG.md", b"# 1.3.8\n")):
            info = tarfile.TarInfo(member)
            info.size = len(data)
            info.mode = 0o755
            tar.addfile(info, io.BytesIO(data))
    return buf.getvalue()


@dataclass
class Cdn:
    body: bytes = field(default_factory=archive)
    modified: str = MODIFIED
    requests: list[str] = field(default_factory=list[str])

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request.method)
        headers = {"Last-Modified": self.modified}
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers)
        return httpx.Response(200, content=self.body, headers=headers)


async def version_ok(argv: list[str]) -> tuple[int, str]:
    assert argv[1] == "--version"
    return 0, "soloist 1.3.8.78 build 1790359283 (20260925) (g5c3a2053ac) (linux/x86_64)\n"


def installer(
    tmp_path: Path, cdn: Cdn, *, now: dt.datetime = NOW, run: Any = version_ok
) -> Installer:
    http = httpx.AsyncClient(transport=httpx.MockTransport(cdn.handler))
    return Installer(
        SoloistPaths(tmp_path / "soloist"), http, now=lambda: now, machine="x86_64", run=run
    )


async def test_first_install(tmp_path: Path) -> None:
    cdn = Cdn()
    outcome = await installer(tmp_path, cdn).update()
    paths = SoloistPaths(tmp_path / "soloist")
    assert outcome.code == "installed"
    assert paths.binary.read_bytes() == elf()
    assert paths.binary.stat().st_mode & 0o111
    assert paths.current_release() == "20260925181322"
    state = read_state(paths)
    # 90 days after the build (18:01 on 25 Sep, from --version), not after the download
    assert state.build_expires_at == "2026-12-24"
    assert (state.error, state.expired_release) == (None, None)


async def test_same_build_is_not_downloaded_again(tmp_path: Path) -> None:
    cdn = Cdn()
    await installer(tmp_path, cdn).update()
    cdn.requests.clear()
    assert (await installer(tmp_path, cdn).update()).code == "up_to_date"
    assert cdn.requests == ["HEAD"]


async def test_newer_build_waits_until_30_days_before_expiry(tmp_path: Path) -> None:
    """SPEC v0.9 §8.1: fewer restarts; new builds only when the running one gets old."""
    cdn = Cdn()
    await installer(tmp_path, cdn).update()
    cdn.modified = "Mon, 05 Oct 2026 10:00:00 GMT"
    cdn.requests.clear()
    assert (
        await installer(tmp_path, cdn, now=NOW + dt.timedelta(days=10)).update()
    ).code == "later"
    assert cdn.requests == ["HEAD"]
    outcome = await installer(tmp_path, cdn, now=NOW + dt.timedelta(days=61)).update()
    assert outcome.code == "installed"
    assert sorted(p.name for p in (tmp_path / "soloist" / "releases").iterdir()) == [
        "20260925181322", "20261005100000",
    ]  # fmt: skip


async def test_expired_build_is_replaced_at_once(tmp_path: Path) -> None:
    cdn = Cdn()
    await installer(tmp_path, cdn).update()
    paths = SoloistPaths(tmp_path / "soloist")
    state = read_state(paths)
    state.expired_release = state.release
    write_state(paths, state)
    cdn.modified = "Mon, 05 Oct 2026 10:00:00 GMT"
    assert (await installer(tmp_path, cdn).update()).code == "installed"
    assert not read_state(paths).expired


@pytest.mark.parametrize(
    ("body", "code"),
    [
        (archive(elf(machine=183)), "wrong_arch"),  # an arm64 build on x86_64
        (archive(b"#!/bin/sh\necho hi\n"), "not_executable"),
        (archive(name="README"), "bad_archive"),
        (b"<html>not a tarball</html>", "bad_archive"),
    ],
)
async def test_broken_downloads_are_refused(tmp_path: Path, body: bytes, code: str) -> None:
    outcome = await installer(tmp_path, Cdn(body=body)).update()
    assert outcome.code == "failed"
    assert read_state(SoloistPaths(tmp_path / "soloist")).error == code
    assert not (tmp_path / "soloist" / "current").exists()


async def test_failing_smoke_test_keeps_the_old_release(tmp_path: Path) -> None:
    cdn = Cdn()
    await installer(tmp_path, cdn).update()

    async def broken(argv: list[str]) -> tuple[int, str]:
        return 1, "error while loading shared libraries"

    cdn.modified = "Mon, 05 Oct 2026 10:00:00 GMT"
    outcome = await installer(tmp_path, cdn, run=broken).update(force=True)
    assert outcome.code == "failed"
    assert SoloistPaths(tmp_path / "soloist").current_release() == "20260925181322"


def test_build_time_from_version() -> None:
    assert build_time("soloist 1.3.8.78 build 1790359283 (20260925)") == dt.datetime(
        2026, 9, 25, 18, 1, 23, tzinfo=dt.UTC
    )
    assert build_time("soloist 2.0") is None


async def test_downloads_only_over_https() -> None:
    async with soloist_client() as http:
        with pytest.raises(httpx.UnsupportedProtocol):
            await http.get("http://soloist-builds.spotifycdn.com/x.tar.gz")


# --- process -------------------------------------------------------------------------------


def test_soloist_command_line_binds_the_websocket_to_loopback(tmp_path: Path) -> None:
    argv = runner.soloist_argv(SoloistPaths(tmp_path), key="k" * 20, name="Myboxi 4711", port=24879)
    assert argv[argv.index("--ws") + 1] == "127.0.0.1:24879"
    assert argv[argv.index("--device-name") + 1] == "Myboxi 4711"
    assert argv[argv.index("--initial-volume") + 1] == "0"


def test_exit_code_10_marks_the_release_expired(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = SoloistPaths(tmp_path)
    write_state(paths, SoloistState(release="20260925181322"))
    started: list[list[str]] = []

    def fake_run(args: list[str], **_kw: object) -> None:
        started.append(args)

    monkeypatch.setattr(runner.subprocess, "run", fake_run)
    runner.stopped(paths, "0", update_unit=UPDATE_UNIT)
    assert not read_state(paths).expired
    runner.stopped(paths, "10", update_unit=UPDATE_UNIT)
    assert read_state(paths).expired
    assert started == [["systemctl", "--user", "--no-block", "start", UPDATE_UNIT]]


def test_key_is_read_from_the_database(tmp_path: Path) -> None:
    db = Database(connect(tmp_path / "myboxi.db"), tmp_path / "assets", FakeClock())
    assert runner.read_key(tmp_path / "myboxi.db") is None
    StateRepo(db).set_soloist_key("sk-0123456789abcdef")
    assert runner.read_key(tmp_path / "myboxi.db") == "sk-0123456789abcdef"


def test_exec_without_key_or_binary_fails(tmp_path: Path) -> None:
    paths = SoloistPaths(tmp_path)
    assert runner.exec_soloist(paths, key=None, name="x", port=1) == 2
    assert runner.exec_soloist(paths, key="sk-0123456789", name="x", port=1) == 2


# --- supervision ---------------------------------------------------------------------------


@dataclass
class FakeUnits:
    active: set[str] = field(default_factory=set[str])
    calls: list[tuple[str, str]] = field(default_factory=list[tuple[str, str]])

    def run(self, verb: str, unit: str) -> bool:
        if verb != "is-active":
            self.calls.append((verb, unit))
        if verb in ("start", "restart") and unit == UNIT:
            self.active.add(unit)
        elif verb == "stop":
            self.active.discard(unit)
        return verb != "is-active" or unit in self.active


@dataclass
class FakeSoloistPlayer:
    connected_: bool = True
    logged_in: bool = True
    playing_: bool = False

    def connected(self) -> bool:
        return self.connected_

    def playing(self) -> bool:
        return self.playing_


@dataclass
class Supervised:
    service: SoloistService
    units: FakeUnits
    player: FakeSoloistPlayer
    paths: SoloistPaths
    flags: dict[str, bool]
    clock: FakeClock


def install(paths: SoloistPaths, release: str, **state: Any) -> None:
    (paths.releases / release).mkdir(parents=True, exist_ok=True)
    (paths.releases / release / "soloist").write_bytes(elf())
    paths.current.unlink(missing_ok=True)
    paths.current.symlink_to(Path("releases") / release)
    write_state(paths, SoloistState(release=release, build_expires_at="2026-12-24", **state))


@pytest.fixture
def sup(tmp_path: Path) -> Supervised:
    units, player, clock = FakeUnits(), FakeSoloistPlayer(), FakeClock()
    flags = {"enabled": True, "key": True}
    paths = SoloistPaths(tmp_path / "soloist")
    service = SoloistService(
        paths=paths, player=player, clock=clock,  # type: ignore[arg-type]
        enabled=lambda: flags["enabled"], key_set=lambda: flags["key"],
        device_name="Myboxi 4711", units=units,
    )  # fmt: skip
    return Supervised(service, units, player, paths, flags, clock)


async def test_missing_build_asks_the_update_unit_not_too_often(sup: Supervised) -> None:
    await sup.service.reconcile()
    await sup.service.reconcile()
    assert sup.units.calls == [("start", UPDATE_UNIT)]
    assert sup.service.status() == {"installed": False, "device_name": "Myboxi 4711",
                                    "state": "installing"}  # fmt: skip
    assert sup.service.unavailable() == "not_running"
    sup.clock.advance(16 * 60)
    await sup.service.reconcile()
    assert sup.units.calls[-1] == ("start", UPDATE_UNIT)


async def test_installed_build_is_started_and_restarted_on_a_new_one_when_idle(
    sup: Supervised,
) -> None:
    install(sup.paths, "20260925181322")
    await sup.service.reconcile()
    assert sup.units.calls == [("start", UNIT)]
    install(sup.paths, "20261005100000")
    sup.player.playing_ = True
    await sup.service.reconcile()
    assert sup.units.calls == [("start", UNIT)]  # not while Spotify plays
    sup.player.playing_ = False
    await sup.service.reconcile()
    assert sup.units.calls[-1] == ("restart", UNIT)


async def test_new_key_restarts_soloist(sup: Supervised) -> None:
    install(sup.paths, "20260925181322")
    await sup.service.reconcile()
    sup.service.key_changed()
    await sup.service.reconcile()
    assert sup.units.calls[-1] == ("restart", UNIT)


async def test_spotify_off_or_no_key_stops_soloist(sup: Supervised) -> None:
    install(sup.paths, "20260925181322")
    await sup.service.reconcile()
    sup.flags["key"] = False
    await sup.service.reconcile()
    assert sup.units.calls[-1] == ("stop", UNIT)
    assert sup.service.unavailable() == "not_configured"
    assert sup.service.status() == {"installed": True, "build_expires_at": "2026-12-24",
                                    "device_name": "Myboxi 4711", "state": "no_key"}  # fmt: skip
    sup.flags.update(enabled=False, key=True)
    assert sup.service.status() == {"installed": True, "build_expires_at": "2026-12-24"}


async def test_expired_build(sup: Supervised) -> None:
    install(sup.paths, "20260925181322", expired_release="20260925181322")
    sup.units.active.add(UNIT)
    await sup.service.reconcile()
    assert sup.units.calls == [("stop", UNIT), ("start", UPDATE_UNIT)]
    assert sup.service.unavailable() == "expired"
    status = sup.service.status()
    assert status is not None
    assert status["state"] == "expired"


async def test_ready_and_login_state(sup: Supervised) -> None:
    install(sup.paths, "20260925181322")
    sup.player.logged_in = False
    assert sup.service.unavailable() == "not_logged_in"
    status = sup.service.status()
    assert status is not None
    assert (status["state"], status["logged_in"]) == ("ready", False)
    sup.player.logged_in = True
    assert sup.service.unavailable() is None
    sup.player.connected_ = False
    assert sup.service.unavailable() == "not_running"


# --- resolution ----------------------------------------------------------------------------


def spotify_snapshot() -> StateResponse:
    token_id, content_id = uuid.uuid4(), uuid.uuid4()
    return StateResponse.model_validate(
        {
            "full": True, "config_rev": 2, "device_rev": 1,
            "upserts": {
                "token": [{"id": str(token_id), "uid": "04A2B3C4D5E680", "label": "Bär"}],
                "content": [{"id": str(content_id), "kind": "spotify", "title": "Album", "rev": 1,
                             "source": {"uri": URI}}],
                "binding": [{"token_id": str(token_id), "content_id": str(content_id),
                             "shuffle": True, "repeat": "all"}],
            },
            "device_config": {"providers_enabled": ["local", "podcast", "spotify"]},
        }
    )  # fmt: skip


def test_spotify_figure_resolution(tmp_path: Path) -> None:
    db = Database(connect(tmp_path / "myboxi.db"), tmp_path / "assets", FakeClock())
    lib = LibraryRepo(db)
    lib.activate(spotify_snapshot())
    res = lib.resolve("04A2B3C4D5E680")
    assert isinstance(res, Unavailable)
    assert (res.provider, res.code) == ("spotify", "disabled")
    StateRepo(db).apply_device_config(
        DeviceConfig(providers_enabled=["local", "podcast", "spotify"]), 1
    )
    assert isinstance(lib.resolve("04A2B3C4D5E680"), Unavailable)  # not configured yet
    lib.spotify_unavailable = lambda: None
    plan = lib.resolve("04A2B3C4D5E680")
    assert isinstance(plan, Playable)
    assert (plan.provider, plan.context, plan.items[0].source) == ("spotify", True, URI)
    assert (plan.shuffle, plan.repeat) == (True, "all")


def test_key_survives_unpairing(tmp_path: Path) -> None:
    """SPEC v0.9 §8.1: the key belongs to the box, not to the household on the server."""
    db = Database(connect(tmp_path / "myboxi.db"), tmp_path / "assets", FakeClock())
    state = StateRepo(db)
    state.set_soloist_key("sk-0123456789abcdef")
    state.clear_tenant()
    assert state.soloist_key() == "sk-0123456789abcdef"
    state.set_soloist_key(None)
    assert state.soloist_key() is None


def test_update_only_when_spotify_is_wanted(tmp_path: Path) -> None:
    db_path = tmp_path / "myboxi.db"
    db = Database(connect(db_path), tmp_path / "assets", FakeClock())
    state = StateRepo(db)
    assert not runner.spotify_wanted(db_path)
    state.set_soloist_key("sk-0123456789abcdef")
    assert not runner.spotify_wanted(db_path)  # default providers: local, podcast
    state.apply_device_config(DeviceConfig(providers_enabled=["local", "spotify"]), 1)
    assert runner.spotify_wanted(db_path)
