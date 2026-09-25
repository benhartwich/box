"""Software updates of the box (SPEC v0.7 §11.1): signed manifest, atomic switch, rollback."""

from __future__ import annotations

import datetime as dt
import hashlib
import io
import json
import shutil
import subprocess
import tarfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import pytest

from myboxi_agent.adapters.bundle import sim_adapters
from myboxi_agent.app import App
from myboxi_agent.config import Settings
from myboxi_agent.update.installer import Paths, Updater
from myboxi_agent.update.manifest import verify_signature

pytestmark = pytest.mark.skipif(shutil.which("openssl") is None, reason="openssl not installed")

URL = "https://updates.test/channel-stable/manifest.json"
BUNDLE_URL = "https://updates.test/myboxi-agent-{v}-arm64.tar.xz"


def _openssl(*args: str) -> None:
    subprocess.run(["openssl", *args], check=True, capture_output=True)


@pytest.fixture(scope="module")
def keys(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    """(private key, directory with the public key)."""
    d = tmp_path_factory.mktemp("keys")
    _openssl("genpkey", "-algorithm", "ed25519", "-out", str(d / "private.pem"))
    trusted = d / "trusted"
    trusted.mkdir()
    _openssl("pkey", "-in", str(d / "private.pem"), "-pubout", "-out", str(trusted / "main.pem"))
    return d / "private.pem", trusted


def sign(private: Path, data: bytes, tmp: Path) -> bytes:
    (tmp / "m.json").write_bytes(data)
    _openssl("pkeyutl", "-sign", "-inkey", str(private), "-rawin",
             "-in", str(tmp / "m.json"), "-out", str(tmp / "m.sig"))  # fmt: skip
    return (tmp / "m.sig").read_bytes()


def bundle_bytes(version: str, *, extra: str | None = None) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:xz") as tar:
        for name, content in ((f"{version}/VERSION", version), (f"{version}/.venv/bin/app", "")):
            data = content.encode()
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
        if extra:
            info = tarfile.TarInfo(extra)
            tar.addfile(info, io.BytesIO(b""))
    return buf.getvalue()


@dataclass
class Channel:
    """The update server: manifest, signature and bundle, with Range support."""

    files: dict[str, bytes] = field(default_factory=dict[str, bytes])
    requests: list[httpx.Request] = field(default_factory=list[httpx.Request])

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        body = self.files.get(str(request.url))
        if body is None:
            return httpx.Response(404)
        if rng := request.headers.get("range"):
            start = int(rng.removeprefix("bytes=").split("-")[0])
            return httpx.Response(206, content=body[start:])
        return httpx.Response(200, content=body)

    def publish(
        self, private: Path, tmp: Path, version: str, *, bundle: bytes | None = None,
        sha: str | None = None,
    ) -> None:  # fmt: skip
        data = bundle if bundle is not None else bundle_bytes(version)
        url = BUNDLE_URL.format(v=version)
        manifest = json.dumps({
            "channel": "stable", "version": version, "released_at": "2026-09-25T12:00:00Z",
            "bundle": {"url": url, "sha256": sha or hashlib.sha256(data).hexdigest(),
                       "size": len(data)},
        }).encode()  # fmt: skip
        self.files[URL] = manifest
        self.files[URL + ".sig"] = sign(private, manifest, tmp)
        self.files[url] = data


@dataclass
class Box:
    """The box around the updater: installed releases, the running agent and the clock."""

    root: Path
    playing: list[bool] = field(default_factory=lambda: [False])
    healthy_versions: set[str] = field(default_factory=lambda: {"0.3.0", "0.3.1", "0.4.0"})
    running: str | None = "0.3.0"
    commands: list[list[str]] = field(default_factory=list[list[str]])
    now: float = 0.0

    def install(self, version: str) -> None:
        release = self.root / "opt" / "releases" / version
        (release / ".venv" / "bin").mkdir(parents=True, exist_ok=True)
        (self.root / "opt" / "current").unlink(missing_ok=True)
        (self.root / "opt" / "current").symlink_to(Path("releases") / version)

    def current(self) -> str:
        return (self.root / "opt" / "current").readlink().name

    async def status(self) -> dict[str, Any] | None:
        if self.running is None:
            return None
        playing = self.playing.pop(0) if len(self.playing) > 1 else self.playing[0]
        return {
            "ok": True,
            "version": self.running,
            "playback": "playing" if playing else "stopped",
        }

    def run(self, cmd: list[str]) -> int:
        self.commands.append(cmd)
        if "restart" in cmd:
            version = self.current()
            self.running = version if version in self.healthy_versions else None
        return 0

    async def sleep(self, seconds: float) -> None:
        self.now += seconds

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def box(tmp_path: Path) -> Box:
    b = Box(tmp_path)
    b.install("0.3.0")
    return b


def make_updater(box: Box, channel: Channel, keys_dir: Path) -> Updater:
    root = box.root
    return Updater(
        paths=Paths(
            install_dir=root / "opt",
            work_dir=root / "work",
            state_file=root / "data" / "update-state.json",
            db_path=root / "data" / "myboxi.db",
            keys_dir=keys_dir,
            reboot_flag=root / "reboot-required",
        ),
        manifest_url=URL,
        http=httpx.AsyncClient(transport=httpx.MockTransport(channel.handler)),
        status=box.status,
        run=box.run,
        sleep=box.sleep,
        monotonic=box.monotonic,
        fallback_version="0.3.0",
        idle_wait_max_s=300,
        idle_poll_s=30,
    )


def state(box: Box) -> dict[str, Any]:
    return json.loads((box.root / "data" / "update-state.json").read_text())


async def test_installs_a_newer_version_atomically(
    box: Box, keys: tuple[Path, Path], tmp_path: Path
) -> None:
    box.install("0.2.0")  # an older release that is no longer needed
    box.install("0.3.0")
    channel = Channel()
    channel.publish(keys[0], tmp_path, "0.3.1")
    await make_updater(box, channel, keys[1]).run_once()
    assert box.current() == "0.3.1"
    assert box.running == "0.3.1"
    assert state(box)["state"] == "installed"
    assert sorted(p.name for p in (box.root / "opt" / "releases").iterdir()) == ["0.3.0", "0.3.1"]
    assert ["systemctl", "--user", "-M", "myboxi@", "restart", "myboxi-agent.service"] in (
        box.commands
    )


async def test_bad_signature_installs_nothing(
    box: Box, keys: tuple[Path, Path], tmp_path: Path
) -> None:
    channel = Channel()
    channel.publish(keys[0], tmp_path, "0.3.1")
    channel.files[URL] = channel.files[URL].replace(b"0.3.1", b"0.9.9")  # tampered
    await make_updater(box, channel, keys[1]).run_once()
    assert state(box) | {"at": None} == {
        "state": "failed", "version": None, "code": "bad_signature", "at": None
    }  # fmt: skip
    assert box.current() == "0.3.0"
    assert not any("tar.xz" in str(r.url) for r in channel.requests)


async def test_wrong_checksum_is_rejected(
    box: Box, keys: tuple[Path, Path], tmp_path: Path
) -> None:
    channel = Channel()
    channel.publish(keys[0], tmp_path, "0.3.1", sha="0" * 64)
    await make_updater(box, channel, keys[1]).run_once()
    assert (state(box)["state"], state(box)["code"]) == ("failed", "checksum")
    assert box.current() == "0.3.0"
    assert not (box.root / "opt" / "releases" / "0.3.1").exists()


@pytest.mark.parametrize("version", ["0.3.0", "0.2.9"])
async def test_never_the_same_or_older(
    box: Box, keys: tuple[Path, Path], tmp_path: Path, version: str
) -> None:
    channel = Channel()
    channel.publish(keys[0], tmp_path, version)
    await make_updater(box, channel, keys[1]).run_once()
    assert state(box)["state"] == "up_to_date"
    assert box.current() == "0.3.0"


async def test_waits_until_nothing_plays(box: Box, keys: tuple[Path, Path], tmp_path: Path) -> None:
    channel = Channel()
    channel.publish(keys[0], tmp_path, "0.3.1")
    box.playing = [True]  # a story plays for longer than the updater waits
    updater = make_updater(box, channel, keys[1])
    await updater.run_once()
    assert state(box)["state"] == "waiting"
    assert box.current() == "0.3.0"
    box.playing = [True, True, False]  # next run: the story ends
    await updater.run_once()
    assert box.current() == "0.3.1"
    assert state(box)["state"] == "installed"
    assert channel.requests[-1].url != BUNDLE_URL.format(v="0.3.1")  # unpacked once only


async def test_unhealthy_version_is_rolled_back_and_not_retried(
    box: Box, keys: tuple[Path, Path], tmp_path: Path
) -> None:
    box.healthy_versions = {"0.3.0"}
    channel = Channel()
    channel.publish(keys[0], tmp_path, "0.3.1")
    updater = make_updater(box, channel, keys[1])
    await updater.run_once()
    assert box.current() == "0.3.0"
    assert box.running == "0.3.0"
    assert (state(box)["state"], state(box)["code"]) == ("rolled_back", "unhealthy")
    restarts = sum("restart" in c for c in box.commands)
    await updater.run_once()
    assert sum("restart" in c for c in box.commands) == restarts  # same version: left alone
    channel.publish(keys[0], tmp_path, "0.4.0")
    box.healthy_versions.add("0.4.0")
    await updater.run_once()
    assert box.current() == "0.4.0"


async def test_auto_update_off_only_downloads(
    box: Box, keys: tuple[Path, Path], tmp_path: Path
) -> None:
    import sqlite3

    db = box.root / "data" / "myboxi.db"
    db.parent.mkdir(parents=True)
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE device_config (id INTEGER PRIMARY KEY, config TEXT)")
    con.execute("INSERT INTO device_config VALUES (1, ?)", (json.dumps({"auto_update": False}),))
    con.commit()
    con.close()
    channel = Channel()
    channel.publish(keys[0], tmp_path, "0.3.1")
    await make_updater(box, channel, keys[1]).run_once()
    assert (state(box)["state"], state(box)["version"]) == ("available", "0.3.1")
    assert box.current() == "0.3.0"
    assert (box.root / "opt" / "releases" / "0.3.1" / "VERSION").is_file()


@pytest.mark.parametrize("healthy", [True, False])
async def test_power_cut_after_the_switch(
    box: Box, keys: tuple[Path, Path], tmp_path: Path, healthy: bool
) -> None:
    """The box restarted with the new version before the health check finished."""
    box.install("0.3.1")
    box.running = "0.3.1" if healthy else None
    box.healthy_versions = {"0.3.0", "0.3.1"} if healthy else {"0.3.0"}
    data = box.root / "data"
    data.mkdir(parents=True)
    (data / "update-state.json").write_text(
        json.dumps({"state": "installing", "version": "0.3.1", "previous": "0.3.0"})
    )
    channel = Channel()
    channel.publish(keys[0], tmp_path, "0.3.1")
    await make_updater(box, channel, keys[1]).run_once()
    assert box.current() == ("0.3.1" if healthy else "0.3.0")
    assert state(box)["state"] == ("installed" if healthy else "rolled_back")


async def test_download_resumes(box: Box, keys: tuple[Path, Path], tmp_path: Path) -> None:
    channel = Channel()
    channel.publish(keys[0], tmp_path, "0.3.1")
    data = channel.files[BUNDLE_URL.format(v="0.3.1")]
    work = box.root / "work"
    work.mkdir()
    (work / "myboxi-agent-0.3.1.tar.xz.part").write_bytes(data[:100])
    await make_updater(box, channel, keys[1]).run_once()
    bundle_requests = [r for r in channel.requests if r.url.path.endswith(".tar.xz")]
    assert bundle_requests[0].headers["range"] == "bytes=100-"
    assert box.current() == "0.3.1"


async def test_bundle_must_stay_in_its_directory(
    box: Box, keys: tuple[Path, Path], tmp_path: Path
) -> None:
    channel = Channel()
    channel.publish(keys[0], tmp_path, "0.3.1", bundle=bundle_bytes("0.3.1", extra="../evil"))
    await make_updater(box, channel, keys[1]).run_once()
    assert (state(box)["state"], state(box)["code"]) == ("failed", "bad_bundle")
    assert not (box.root / "opt" / "evil").exists()


async def test_reboot_for_system_updates_only_when_quiet(
    box: Box, keys: tuple[Path, Path], tmp_path: Path
) -> None:
    channel = Channel()
    channel.publish(keys[0], tmp_path, "0.3.0")
    (box.root / "reboot-required").touch()
    box.playing = [True]
    updater = make_updater(box, channel, keys[1])
    await updater.run_once()
    assert ["systemctl", "reboot"] not in box.commands
    box.playing = [False]
    updater.idle_wait_max_s = 3600
    await updater.run_once()
    assert ["systemctl", "reboot"] in box.commands


def test_signature_needs_a_trusted_key(keys: tuple[Path, Path], tmp_path: Path) -> None:
    data = b'{"version": "0.3.1"}'
    signature = sign(keys[0], data, tmp_path)
    assert verify_signature(data, signature, keys[1])
    assert not verify_signature(data + b" ", signature, keys[1])
    empty = tmp_path / "none"
    empty.mkdir()
    assert not verify_signature(data, signature, empty)


def test_agent_reports_the_update_state(tmp_path: Path) -> None:
    app = App(Settings(data_dir=tmp_path, sim=True), sim_adapters())
    assert app.reported_data().update is None
    assert app.status()["version"]
    (tmp_path / "update-state.json").write_text(
        json.dumps({"state": "installing", "version": "0.3.1", "previous": "0.3.0",
                    "at": dt.datetime.now(dt.UTC).isoformat()})
    )  # fmt: skip
    update = app.reported_data().update
    assert update is not None
    assert (update.state, update.version) == ("waiting", "0.3.1")
    (tmp_path / "update-state.json").write_text(
        json.dumps({"state": "rolled_back", "version": "0.3.1", "code": "unhealthy"})
    )
    update = app.reported_data().update
    assert update is not None
    assert (update.state, update.code) == ("rolled_back", "unhealthy")
