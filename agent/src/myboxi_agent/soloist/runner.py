"""The Soloist process (user unit ``myboxi-soloist.service``, SPEC v0.9 §8.1).

``exec`` replaces itself with Soloist: the key comes from the box database and goes onto the
command line, the only way Soloist accepts it (SPEC v0.9 §10). Nothing here logs the argument
list. ``stopped`` runs after Soloist ended (``ExecStopPost``): exit code 10 means the build
expired, which marks the release and asks for an update at once.
"""

from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

from myboxi_agent.soloist.install import SoloistPaths, read_state, write_state

log = logging.getLogger(__name__)

EXIT_EXPIRED = 10
CACHE_MB = 200


def device_name(serial_name: str) -> str:
    """``Myboxi-4711`` (setup network) → ``Myboxi 4711`` (Spotify Connect)."""
    return serial_name.replace("-", " ")


def soloist_argv(paths: SoloistPaths, *, key: str, name: str, port: int) -> list[str]:
    return [
        str(paths.binary),
        "--device-name", name,
        "--api-key", key,
        "--data-dir", str(paths.data),
        "--cache-dir", str(paths.cache),
        "--cache-size", str(CACHE_MB),
        "--ws", f"127.0.0.1:{port}",  # SPEC §10: never another address
        "--initial-volume", "0",  # the agent sets the volume through its policy
    ]  # fmt: skip


def exec_soloist(paths: SoloistPaths, *, key: str | None, name: str, port: int) -> int:
    if not key:
        log.error("no Soloist API key on this box")
        return 2
    if not paths.binary.is_file():
        log.error("Soloist is not installed")
        return 2
    for directory in (paths.data, paths.cache):
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    argv = soloist_argv(paths, key=key, name=name, port=port)
    os.execv(argv[0], argv)  # noqa: S606 - fixed program, no shell
    return 0  # not reached: execv replaces the process


def stopped(paths: SoloistPaths, exit_status: str | None, *, update_unit: str) -> int:
    if exit_status != str(EXIT_EXPIRED):
        return 0
    state = read_state(paths)
    state.expired_release = state.release or paths.current_release()
    write_state(paths, state)
    log.warning("soloist build expired", extra={"release": state.expired_release})
    # Fixed argument list, no shell.
    subprocess.run(  # noqa: S603
        ["systemctl", "--user", "--no-block", "start", update_unit],  # noqa: S607
        check=False,
        timeout=30,
    )
    return 0


def spotify_wanted(db_path: Path) -> bool:
    """Spotify is enabled for the box and a key is stored; otherwise nothing is downloaded."""
    import sqlite3

    from pydantic import ValidationError

    from myboxi_protocol.state import DeviceConfig

    if read_key(db_path) is None:
        return False
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            row = conn.execute("SELECT config FROM device_config WHERE id = 1").fetchone()
        finally:
            conn.close()
    except sqlite3.Error:
        return False
    try:
        config = DeviceConfig.model_validate_json(row[0]) if row else DeviceConfig()
    except ValidationError:
        return False
    return "spotify" in config.providers_enabled


def read_key(db_path: Path) -> str | None:
    """Read-only look at ``secret`` (the agent owns the database)."""
    import sqlite3

    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    except sqlite3.Error:
        return None
    try:
        row = conn.execute("SELECT value FROM secret WHERE name = 'soloist_api_key'").fetchone()
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    return str(row[0]) if row else None
