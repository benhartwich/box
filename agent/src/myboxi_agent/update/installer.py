"""Install agent updates atomically and roll back unhealthy ones (SPEC v0.7 §11.1).

Layout on the box::

    /opt/myboxi-agent/releases/<version>/.venv, prompts/   one directory per version
    /opt/myboxi-agent/current -> releases/<version>        what the services run

Every step can be interrupted by a power cut: a release directory appears only complete
(extracted next to it, then renamed), ``current`` changes with one ``rename`` and the state
file remembers an unfinished switch, so the next run checks the health or rolls back.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import hashlib
import json
import logging
import shutil
import sqlite3
import tarfile
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import httpx

from myboxi_agent.update.manifest import ManifestError, fetch_manifest
from myboxi_protocol.updates import UpdateManifest, is_newer

log = logging.getLogger(__name__)

HEALTH_TIMEOUT_S = 90.0
IDLE_POLL_S = 30.0
IDLE_WAIT_MAX_S = 2 * 3600.0
REBOOT_IDLE_S = 600.0
CHUNK = 256 * 1024

Status = Callable[[], Awaitable[dict[str, Any] | None]]
Runner = Callable[[list[str]], int]
Sleep = Callable[[float], Awaitable[None]]
Monotonic = Callable[[], float]


def read_state(path: Path) -> dict[str, Any] | None:
    """The state file as a JSON object, None when missing or broken."""
    try:
        data: object = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    return cast(dict[str, Any], data) if isinstance(data, dict) else None


class UpdateFailed(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass
class Paths:
    install_dir: Path  # /opt/myboxi-agent
    work_dir: Path  # downloads
    state_file: Path  # read by the agent for ``reported.update``
    db_path: Path  # the agent's database: device_config.auto_update
    keys_dir: Path
    reboot_flag: Path = Path("/run/reboot-required")

    @property
    def releases(self) -> Path:
        return self.install_dir / "releases"

    @property
    def current(self) -> Path:
        return self.install_dir / "current"


@dataclass
class Updater:
    paths: Paths
    manifest_url: str
    http: httpx.AsyncClient
    status: Status  # the running agent's control socket status, None when unreachable
    run: Runner
    sleep: Sleep
    monotonic: Monotonic
    fallback_version: str  # version of a box without the releases layout
    agent_user: str = "myboxi"
    health_timeout_s: float = HEALTH_TIMEOUT_S
    idle_wait_max_s: float = IDLE_WAIT_MAX_S
    idle_poll_s: float = IDLE_POLL_S
    reboot_idle_s: float = REBOOT_IDLE_S
    _state: dict[str, Any] = field(default_factory=dict[str, Any])

    # --- state file ------------------------------------------------------------------------

    def load_state(self) -> dict[str, Any]:
        return read_state(self.paths.state_file) or {}

    def save_state(self, state: str, version: str | None = None, **extra: Any) -> None:
        keep = {k: self._state[k] for k in ("failed_version",) if k in self._state}
        self._state = keep | {
            "state": state,
            "version": version,
            "at": dt.datetime.now(dt.UTC).isoformat(),
        } | extra  # fmt: skip
        tmp = self.paths.state_file.with_suffix(".tmp")
        tmp.parent.mkdir(parents=True, exist_ok=True)
        tmp.write_text(json.dumps(self._state))
        tmp.chmod(0o644)
        tmp.replace(self.paths.state_file)
        log.info("update state", extra={"state": state, "version": version, **extra})

    # --- versions ------------------------------------------------------------------------

    def current_version(self) -> str:
        try:
            return self.paths.current.readlink().name
        except OSError:
            return self.fallback_version

    def auto_update(self) -> bool:
        """SPEC v0.7 §3.4, from the agent's database (read only)."""
        try:
            con = sqlite3.connect(f"file:{self.paths.db_path}?mode=ro", uri=True, timeout=5)
            try:
                row = con.execute("SELECT config FROM device_config WHERE id = 1").fetchone()
            finally:
                con.close()
        except sqlite3.Error:
            return True
        if row is None:
            return True
        try:
            return bool(json.loads(row[0]).get("auto_update", True))
        except (ValueError, AttributeError):
            return True

    # --- the run -------------------------------------------------------------------------

    async def run_once(self) -> None:
        self._state = self.load_state()
        if self._state.get("state") == "installing":
            await self._finish_interrupted()
        try:
            await self._update()
        finally:
            await self.maybe_reboot()

    async def _update(self) -> None:
        current = self.current_version()
        try:
            manifest = await fetch_manifest(self.http, self.manifest_url, self.paths.keys_dir)
        except httpx.HTTPError as exc:
            log.info("no update manifest (offline?)", extra={"error": str(exc)})
            return
        except ManifestError as exc:
            self.save_state("failed", None, code=exc.code)
            return
        version = manifest.version
        if not is_newer(version, current):
            if self._state.get("state") not in ("installed", "rolled_back"):
                self.save_state("up_to_date", current)
            return
        if self._state.get("failed_version") == version:
            return  # rolled back before: wait for a newer release
        try:
            release = await self._prepare(manifest)
        except UpdateFailed as exc:
            self.save_state("failed", version, code=exc.code)
            return
        if not self.auto_update():
            self.save_state("available", version)
            return
        self.save_state("waiting", version)
        if not await self.wait_idle(0):
            return  # still playing: the next run tries again
        await self._switch(release, version, current)

    async def _prepare(self, manifest: UpdateManifest) -> Path:
        release = self.paths.releases / manifest.version
        if release.is_dir():
            return release
        self.save_state("downloading", manifest.version)
        free = shutil.disk_usage(self.paths.install_dir).free
        if free < manifest.bundle.size * 5 + 50 * 1024 * 1024:
            raise UpdateFailed("no_space")
        bundle = await self.download(manifest)
        try:
            return self.unpack(bundle, manifest.version)
        finally:
            bundle.unlink(missing_ok=True)

    async def _switch(self, release: Path, version: str, previous: str) -> None:
        self.save_state("installing", version, previous=previous)
        self.point_current(release)
        self.restart_agent()
        if await self.healthy(version):
            self.save_state("installed", version)
            self.cleanup(keep={version, previous})
            return
        await self._roll_back(version, previous)

    async def _roll_back(self, version: str, previous: str) -> None:
        log.error("new version not healthy, rolling back", extra={"version": version})
        old = self.paths.releases / previous
        if old.is_dir():
            self.point_current(old)
            self.restart_agent()
        self._state["failed_version"] = version
        self.save_state("rolled_back", version, code="unhealthy")

    async def _finish_interrupted(self) -> None:
        """Power cut between switching and the health check: check now."""
        version, previous = self._state.get("version"), self._state.get("previous")
        if not isinstance(version, str) or not isinstance(previous, str):
            return
        if self.current_version() != version:
            self.save_state("waiting", version)  # cut before the switch: try again
            return
        if await self.healthy(version):
            self.save_state("installed", version)
        else:
            await self._roll_back(version, previous)

    # --- steps ---------------------------------------------------------------------------

    async def download(self, manifest: UpdateManifest) -> Path:
        work = self.paths.work_dir
        work.mkdir(parents=True, exist_ok=True)
        part = work / f"myboxi-agent-{manifest.version}.tar.xz.part"
        have = part.stat().st_size if part.exists() else 0
        headers = {"Range": f"bytes={have}-"} if 0 < have < manifest.bundle.size else {}
        try:
            async with self.http.stream(
                "GET", manifest.bundle.url, headers=headers, follow_redirects=True
            ) as response:
                if response.status_code == 200:
                    have = 0
                elif response.status_code != 206:
                    raise UpdateFailed("download")
                with part.open("r+b" if have else "wb") as fh:
                    fh.seek(have)
                    fh.truncate()
                    async for chunk in response.aiter_bytes(CHUNK):
                        have += len(chunk)
                        if have > manifest.bundle.size:
                            raise UpdateFailed("checksum")
                        fh.write(chunk)
        except httpx.HTTPError:
            raise UpdateFailed("download") from None
        digest = hashlib.sha256()
        with part.open("rb") as fh:
            while block := fh.read(CHUNK):
                digest.update(block)
        if have != manifest.bundle.size or digest.hexdigest() != manifest.bundle.sha256:
            part.unlink(missing_ok=True)
            raise UpdateFailed("checksum")
        done = work / f"myboxi-agent-{manifest.version}.tar.xz"
        part.replace(done)
        return done

    def unpack(self, bundle: Path, version: str) -> Path:
        releases = self.paths.releases
        releases.mkdir(parents=True, exist_ok=True)
        staging = releases / f".partial-{version}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        try:
            with tarfile.open(bundle, "r:xz") as tar:
                for member in tar.getmembers():
                    name = member.name.removeprefix("./")
                    if name != version and not name.startswith(f"{version}/"):
                        raise UpdateFailed("bad_bundle")
                tar.extractall(staging, filter="tar", numeric_owner=True)
        except (tarfile.TarError, OSError, EOFError, LookupError):
            shutil.rmtree(staging, ignore_errors=True)
            raise UpdateFailed("bad_bundle") from None
        except UpdateFailed:
            shutil.rmtree(staging, ignore_errors=True)
            raise
        target = releases / version
        (staging / version).replace(target)
        shutil.rmtree(staging, ignore_errors=True)
        return target

    def point_current(self, release: Path) -> None:
        tmp = self.paths.install_dir / ".current.new"
        with contextlib.suppress(FileNotFoundError):
            tmp.unlink()
        tmp.symlink_to(Path("releases") / release.name)
        tmp.replace(self.paths.current)

    def restart_agent(self) -> None:
        code = self.run(
            ["systemctl", "--user", "-M", f"{self.agent_user}@", "restart", "myboxi-agent.service"]
        )
        if code != 0:
            log.error("could not restart the agent", extra={"code": code})

    async def healthy(self, version: str) -> bool:
        deadline = self.monotonic() + self.health_timeout_s
        while self.monotonic() < deadline:
            status = await self.status()
            if status and status.get("ok") and status.get("version") == version:
                return True
            await self.sleep(2.0)
        return False

    async def wait_idle(self, for_s: float) -> bool:
        """True once nothing has been playing for ``for_s`` seconds (paused counts as idle)."""
        deadline = self.monotonic() + self.idle_wait_max_s
        idle_since: float | None = None
        while True:
            status = await self.status()
            now = self.monotonic()
            playing = bool(status and status.get("playback") == "playing")
            if playing:
                idle_since = None
            elif idle_since is None:
                idle_since = now
            if idle_since is not None and now - idle_since >= for_s:
                return True
            if now >= deadline:
                return False
            await self.sleep(self.idle_poll_s)

    def cleanup(self, keep: set[str]) -> None:
        for entry in self.paths.releases.iterdir():
            if entry.name not in keep and not entry.name.startswith("."):
                shutil.rmtree(entry, ignore_errors=True)

    async def maybe_reboot(self) -> None:
        """Security updates that need a reboot: only after 10 quiet minutes (SPEC §11.1)."""
        if not self.paths.reboot_flag.exists():
            return
        if await self.wait_idle(self.reboot_idle_s):
            log.info("rebooting for system updates")
            self.run(["systemctl", "reboot"])
