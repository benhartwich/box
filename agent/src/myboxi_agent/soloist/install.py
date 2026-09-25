"""Soloist install and update (SPEC v0.9 §8.1), run by ``myboxi-agent soloist update``.

Soloist may not be redistributed, so it never ships in the repository or image (CLAUDE.md):
the box downloads it from Spotify's official URL over HTTPS (Spotify publishes no checksums),
checks that it is an executable for this machine and that ``--version`` works, installs it
under ``releases/<build>/`` and switches the ``current`` symlink. Builds expire 90 days after
their build date, which is never later than the archive's ``Last-Modified``.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import email.utils
import io
import json
import logging
import platform
import re
import shutil
import tarfile
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import httpx

log = logging.getLogger(__name__)

URL = "https://soloist-builds.spotifycdn.com/soloist_release_{arch}.tar.gz"
LIFETIME = dt.timedelta(days=90)
RENEW_BEFORE = dt.timedelta(days=30)
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_BINARY = 256 * 1024 * 1024
KEEP_RELEASES = 2
SMOKE_TIMEOUT_S = 20
_BUILD = re.compile(r"\bbuild (\d{9,11})\b")  # "soloist 1.3.8.78 build 1790359283 (20260925)"
# platform.machine() → (archive name, ELF class, ELF e_machine)
ARCHES = {
    "aarch64": ("arm64", 2, 183),
    "arm64": ("arm64", 2, 183),
    "armv7l": ("arm32", 1, 40),
    "x86_64": ("x86_64", 2, 62),
}


class InstallError(Exception):
    """``code`` goes into the state file and, as ``failed``, into ``reported.soloist``."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class SoloistPaths:
    base: Path

    @property
    def releases(self) -> Path:
        return self.base / "releases"

    @property
    def current(self) -> Path:
        return self.base / "current"

    @property
    def binary(self) -> Path:
        return self.current / "soloist"

    @property
    def data(self) -> Path:
        return self.base / "data"

    @property
    def cache(self) -> Path:
        return self.base / "cache"

    @property
    def state_file(self) -> Path:
        return self.base / "state.json"

    def current_release(self) -> str | None:
        try:
            return self.current.readlink().name
        except OSError:
            return None


@dataclass
class SoloistState:
    """Shared by the update job, the stop hook and the agent (all run as ``myboxi``)."""

    release: str | None = None
    last_modified: str | None = None  # HTTP date of the installed archive
    build_expires_at: str | None = None  # ISO date
    checked_at: str | None = None
    error: str | None = None
    expired_release: str | None = None  # the release that exited with code 10
    extra: dict[str, Any] = field(default_factory=dict[str, Any])

    @property
    def expired(self) -> bool:
        return self.release is not None and self.expired_release == self.release

    def expires(self) -> dt.date | None:
        return dt.date.fromisoformat(self.build_expires_at) if self.build_expires_at else None


def read_state(paths: SoloistPaths) -> SoloistState:
    try:
        data = json.loads(paths.state_file.read_text())
    except (OSError, ValueError):
        return SoloistState()
    if not isinstance(data, dict):
        return SoloistState()
    known = {k: v for k, v in data.items() if k in SoloistState.__dataclass_fields__}  # pyright: ignore[reportUnknownVariableType]
    try:
        return SoloistState(**known)  # pyright: ignore[reportUnknownArgumentType]
    except TypeError:
        return SoloistState()


def write_state(paths: SoloistPaths, state: SoloistState) -> None:
    paths.base.mkdir(parents=True, exist_ok=True)
    tmp = paths.state_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(asdict(state), indent=1))
    tmp.replace(paths.state_file)


def arch(machine: str | None = None) -> tuple[str, int, int]:
    machine = machine or platform.machine()
    if machine not in ARCHES:
        raise InstallError("unsupported_arch", machine)
    return ARCHES[machine]


def _http_date(value: str | None) -> dt.datetime | None:
    if not value:
        return None
    try:
        parsed = email.utils.parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)


def build_time(version_output: str) -> dt.datetime | None:
    """The build timestamp ``soloist --version`` prints (Unix seconds)."""
    match = _BUILD.search(version_output)
    if match is None:
        return None
    try:
        return dt.datetime.fromtimestamp(int(match.group(1)), dt.UTC)
    except (OverflowError, OSError, ValueError):
        return None


def check_elf(data: bytes, elf_class: int, machine: int) -> None:
    if len(data) < 20 or data[:4] != b"\x7fELF":
        raise InstallError("not_executable", "not an ELF file")
    if data[4] != elf_class or data[5] != 1 or int.from_bytes(data[18:20], "little") != machine:
        raise InstallError("wrong_arch", "ELF for another machine")


def extract_binary(archive: bytes) -> bytes:
    """The ``soloist`` executable from the archive; nothing else is written to disk."""
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
            for member in tar:
                if member.isfile() and member.name.rsplit("/", 1)[-1] == "soloist":
                    if member.size > MAX_BINARY:
                        raise InstallError("too_large")
                    fh = tar.extractfile(member)
                    if fh is not None:
                        return fh.read()
    except (tarfile.TarError, OSError, EOFError) as exc:
        raise InstallError("bad_archive", str(exc)) from exc
    raise InstallError("bad_archive", "no soloist executable in the archive")


Runner = Callable[[list[str]], Awaitable[tuple[int, str]]]


async def run_version(argv: list[str]) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), SMOKE_TIMEOUT_S)
    except TimeoutError:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
        await proc.wait()
        return -1, ""
    return proc.returncode or 0, out.decode("utf-8", "replace")


@dataclass(frozen=True)
class Outcome:
    code: str  # installed, up_to_date, later, failed
    release: str | None = None


class Installer:
    def __init__(
        self,
        paths: SoloistPaths,
        http: httpx.AsyncClient,
        *,
        now: Callable[[], dt.datetime],
        machine: str | None = None,
        run: Runner = run_version,
    ) -> None:
        self.paths = paths
        self.http = http
        self.now = now
        self.machine = machine
        self.run = run

    async def update(self, *, force: bool = False) -> Outcome:
        state = read_state(self.paths)
        state.checked_at = self.now().isoformat()
        try:
            outcome = await self._update(state, force=force)
        except InstallError as exc:
            state.error = exc.code
            write_state(self.paths, state)
            log.warning("soloist update failed", extra={"code": exc.code})
            return Outcome("failed")
        except httpx.HTTPError as exc:
            state.error = "download"
            write_state(self.paths, state)
            log.warning("soloist download failed", extra={"error": str(exc)[:200]})
            return Outcome("failed")
        state.error = None
        write_state(self.paths, state)
        return outcome

    async def _update(self, state: SoloistState, *, force: bool) -> Outcome:
        name, elf_class, machine = arch(self.machine)
        url = URL.format(arch=name)
        head = await self.http.head(url)
        if head.status_code != 200:
            raise InstallError("download", f"HTTP {head.status_code}")
        modified = _http_date(head.headers.get("last-modified"))
        installed = self.paths.binary.is_file() and state.release is not None
        known = _http_date(state.last_modified)
        newer = modified is None or known is None or modified > known
        if installed and not newer:
            return Outcome("up_to_date", state.release)
        expires = state.expires()
        due = (
            force
            or not installed
            or state.expired
            or expires is None
            or dt.datetime.combine(expires, dt.time(), dt.UTC) - self.now() < RENEW_BEFORE
        )
        if not due:
            return Outcome("later", state.release)
        archive = await self._download(url)
        binary = extract_binary(archive)
        check_elf(binary, elf_class, machine)
        stamp = (modified or self.now()).astimezone(dt.UTC).strftime("%Y%m%d%H%M%S")
        release = self.paths.releases / stamp
        staging = self.paths.releases / f".{stamp}.tmp"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir(parents=True)
        (staging / "soloist").write_bytes(binary)
        (staging / "soloist").chmod(0o755)
        code, out = await self.run([str(staging / "soloist"), "--version"])
        if code != 0:
            shutil.rmtree(staging, ignore_errors=True)
            raise InstallError("smoke_test", out[-200:])
        shutil.rmtree(release, ignore_errors=True)
        staging.rename(release)
        link = self.paths.base / ".current.tmp"
        link.unlink(missing_ok=True)
        link.symlink_to(Path("releases") / stamp)
        link.replace(self.paths.current)  # atomic switch
        self._prune(stamp)
        state.release = stamp
        state.last_modified = head.headers.get("last-modified")
        # The build date from --version; the archive is never older than its build.
        built = min(t for t in (build_time(out), modified, self.now()) if t is not None)
        state.build_expires_at = (built + LIFETIME).date().isoformat()
        state.expired_release = None
        log.info(
            "soloist installed",
            extra={"release": stamp, "version": out.strip().splitlines()[0][:120] if out else ""},
        )
        return Outcome("installed", stamp)

    async def _download(self, url: str) -> bytes:
        chunks: list[bytes] = []
        size = 0
        async with self.http.stream("GET", url) as r:
            if r.status_code != 200:
                raise InstallError("download", f"HTTP {r.status_code}")
            async for chunk in r.aiter_bytes():
                size += len(chunk)
                if size > MAX_ARCHIVE:
                    raise InstallError("too_large")
                chunks.append(chunk)
        return b"".join(chunks)

    def _prune(self, keep: str) -> None:
        releases = sorted(
            (p for p in self.paths.releases.iterdir() if p.is_dir() and not p.name.startswith(".")),
            key=lambda p: p.name,
            reverse=True,
        )
        for old in [p for p in releases if p.name != keep][KEEP_RELEASES - 1 :]:
            shutil.rmtree(old, ignore_errors=True)


def soloist_client() -> httpx.AsyncClient:
    """HTTPS only, also after redirects."""

    async def https_only(request: httpx.Request) -> None:
        if request.url.scheme != "https":
            raise httpx.UnsupportedProtocol("Soloist only over HTTPS")

    return httpx.AsyncClient(
        follow_redirects=True,
        max_redirects=5,
        timeout=httpx.Timeout(60.0, connect=15.0),
        event_hooks={"request": [https_only]},
    )
