"""Command line interface ``myboxi-agent``."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import shutil
import signal
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from myboxi_agent import __version__
from myboxi_agent.config import Settings, get_settings
from myboxi_agent.control import request
from myboxi_agent.logconfig import configure_logging

Command = Callable[[argparse.Namespace, Settings], int]
AUDIO_SUFFIXES = {".opus", ".ogg", ".oga", ".mp3", ".m4a", ".flac", ".wav"}


def _cmd_version(args: argparse.Namespace, settings: Settings) -> int:
    del args, settings
    print(f"myboxi-agent {__version__}")
    return 0


def _cmd_run(args: argparse.Namespace, settings: Settings) -> int:
    from myboxi_agent.app import App

    async def main() -> None:
        app = App(settings)
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, app.stop)
        await app.run()

    asyncio.run(main())
    return 0


def _control(settings: Settings, payload: dict[str, Any]) -> int:
    try:
        result = asyncio.run(request(settings.control_socket, payload))
    except (FileNotFoundError, ConnectionRefusedError):
        print(f"agent not running ({settings.control_socket})", file=sys.stderr)
        return 2
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("ok") else 1


def _cmd_status(args: argparse.Namespace, settings: Settings) -> int:
    del args
    return _control(settings, {"cmd": "status"})


def _cmd_sync(args: argparse.Namespace, settings: Settings) -> int:
    del args
    return _control(settings, {"cmd": "sync_now"})


def _cmd_repair(args: argparse.Namespace, settings: Settings) -> int:
    del args
    return _control(settings, {"cmd": "repair"})


def _cmd_server(args: argparse.Namespace, settings: Settings) -> int:
    return _control(settings, {"cmd": "set_server_url", "url": args.url})


def _cmd_sim(args: argparse.Namespace, settings: Settings) -> int:
    match args.sim_command:
        case "place":
            payload: dict[str, Any] = {"cmd": "place", "uid": args.uid}
        case "remove" | "finish" | "nfc-ok":
            payload = {"cmd": args.sim_command}
        case "nfc-fail":
            payload = {"cmd": "nfc-fail", "code": args.code}
        case "press":
            payload = {"cmd": "press", "button": args.button}
        case "spotify":
            payload = {"cmd": "spotify", "action": args.action, "value": args.value}
        case _:
            payload = {"cmd": "hold", "buttons": args.buttons, "seconds": args.seconds}
    return _control(settings, payload)


def _cmd_library_add(args: argparse.Namespace, settings: Settings) -> int:
    """SPEC v0.5 §4: local library for a box without server (M0)."""
    from myboxi_agent.adapters.clock import SystemClock
    from myboxi_agent.store.db import connect
    from myboxi_agent.store.repos import AssetRepo, Database, LibraryRepo

    folder = Path(args.path)
    files = sorted(p for p in folder.iterdir() if p.suffix.lower() in AUDIO_SUFFIXES)
    if not files:
        print(f"no audio files in {folder}", file=sys.stderr)
        return 1
    db = Database(connect(settings.db_path), settings.asset_dir, SystemClock())
    assets = AssetRepo(db)
    entries: list[tuple[str, int, str, int]] = []
    for f in files:
        sha = hashlib.sha256(f.read_bytes()).hexdigest()
        dest = assets.path(sha)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if not dest.exists():
            shutil.copyfile(f, dest)
        size = dest.stat().st_size
        assets.register(sha, size)
        entries.append((sha, size, f.stem, 0))
    uid = args.uid.replace(":", "").upper()
    LibraryRepo(db).add_local(uid, args.label or folder.name, args.title or folder.name, entries)
    print(f"{uid}: {len(entries)} Titel aus {folder}")
    return 0


def _cmd_setupd(args: argparse.Namespace, settings: Settings) -> int:
    """Root service for setup mode (SPEC §9.3); started by the agent via systemd."""
    from myboxi_agent.setup.daemon import SocketAgentLink, run_setup
    from myboxi_agent.setup.nm import NetworkManager, network_name, read_serial

    async def agent_status() -> dict[str, Any]:
        try:
            return await request(settings.control_socket, {"cmd": "status"}, limit_s=5)
        except (OSError, TimeoutError, ValueError):
            return {}

    async def main() -> bool:
        status = await agent_status()
        url = status.get("server_url") or settings.default_server_url or "https://app.myboxi.eu"
        return await run_setup(
            NetworkManager(),
            SocketAgentLink(settings.control_socket),
            ssid=network_name(read_serial()),
            default_server_url=str(url),
            host=args.host,
            port=args.port,
            soloist_key_set=bool(status.get("soloist_key_set")),
        )

    return 0 if asyncio.run(main()) else 1


def _cmd_update(args: argparse.Namespace, settings: Settings) -> int:
    """SPEC v0.7 §11.1: one update run (root, myboxi-updater.service)."""
    del args
    import time

    import httpx

    from myboxi_agent.adapters.hardware import run_command
    from myboxi_agent.update.installer import Paths, Updater

    async def status() -> dict[str, Any] | None:
        try:
            return await request(settings.control_socket, {"cmd": "status"}, limit_s=5)
        except (OSError, TimeoutError, ValueError):
            return None

    async def main() -> None:
        timeout = httpx.Timeout(30.0, connect=15.0)
        async with httpx.AsyncClient(timeout=timeout) as http:
            updater = Updater(
                paths=Paths(
                    install_dir=settings.install_dir,
                    work_dir=settings.update_work_dir,
                    state_file=settings.update_state_file,
                    db_path=settings.db_path,
                    keys_dir=settings.update_keys_dir,
                ),
                manifest_url=settings.update_manifest_url,
                http=http,
                status=status,
                run=run_command,
                sleep=asyncio.sleep,
                monotonic=time.monotonic,
                fallback_version=__version__,
                agent_user=settings.agent_user,
            )
            await updater.run_once()

    asyncio.run(main())
    return 0


def _cmd_soloist(args: argparse.Namespace, settings: Settings) -> int:
    """Spotify (SPEC v0.9 §8.1): user units myboxi-soloist(-update).service run these."""
    from myboxi_agent.soloist.install import SoloistPaths

    paths = SoloistPaths(settings.soloist_dir)
    match args.soloist_command:
        case "update":
            from myboxi_agent.soloist.runner import spotify_wanted

            if not args.force and not spotify_wanted(settings.db_path):
                print("spotify off")  # the daily timer never downloads Soloist unasked
                return 0
            return _soloist_update(paths, force=args.force)
        case "exec":
            from myboxi_agent.setup.nm import network_name, read_serial
            from myboxi_agent.soloist.runner import device_name, exec_soloist, read_key

            return exec_soloist(
                paths,
                key=read_key(settings.db_path),
                name=device_name(network_name(read_serial())),
                port=settings.soloist_ws_port,
            )
        case "stopped":
            import os

            from myboxi_agent.soloist.runner import stopped

            return stopped(
                paths, os.environ.get("EXIT_STATUS"), update_unit="myboxi-soloist-update.service"
            )
        case "key":
            if args.clear:
                return _control(settings, {"cmd": "clear_soloist_key"})
            # From stdin, never from the command line (it would show up in the process list).
            key = sys.stdin.readline().strip()
            return _control(settings, {"cmd": "set_soloist_key", "key": key})
        case _:
            return 2


def _soloist_update(paths: Any, *, force: bool) -> int:
    import datetime as dt

    from myboxi_agent.soloist.install import Installer, soloist_client

    async def main() -> str:
        async with soloist_client() as http:
            installer = Installer(paths, http, now=lambda: dt.datetime.now(dt.UTC))
            return (await installer.update(force=force)).code

    code = asyncio.run(main())
    print(code)
    return 1 if code == "failed" else 0


def _cmd_doctor(args: argparse.Namespace, settings: Settings) -> int:
    from myboxi_agent.doctor import run_doctor

    return run_doctor(settings, offline=args.offline)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="myboxi-agent")
    parser.add_argument("--version", action="version", version=f"myboxi-agent {__version__}")
    parser.add_argument("--data-dir", type=Path, help="override MYBOXI_AGENT_DATA_DIR")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version").set_defaults(func=_cmd_version)

    p = sub.add_parser("run", help="run the agent")
    p.add_argument("--sim", action="store_true", help="simulated hardware (CLAUDE.md rule 2)")
    p.add_argument("--server-url", help="default server URL (e.g. http://127.0.0.1:8000)")
    p.set_defaults(func=_cmd_run)

    sub.add_parser("status", help="state of the running agent").set_defaults(func=_cmd_status)
    sub.add_parser("sync", help="sync with the server now").set_defaults(func=_cmd_sync)
    sub.add_parser("repair", help="unpair and pair again (SPEC §9.4)").set_defaults(
        func=_cmd_repair
    )
    p = sub.add_parser("server", help="set the server URL (pairs again if it changes)")
    p.add_argument("url")
    p.set_defaults(func=_cmd_server)

    p = sub.add_parser("sim", help="drive a simulated agent (run --sim)")
    ss = p.add_subparsers(dest="sim_command", required=True)
    ss.add_parser("place", help="place a figure").add_argument("uid")
    ss.add_parser("remove", help="remove the figure")
    ss.add_parser("finish", help="let the playlist reach its end")
    ss.add_parser("nfc-fail", help="simulate a broken NFC reader (self-test)").add_argument(
        "--code", default="not_responding", choices=["no_i2c", "not_responding", "read_error"]
    )
    ss.add_parser("nfc-ok", help="the simulated NFC reader works again")
    ss.add_parser("press", help="press a button").add_argument(
        "button", choices=["play_pause", "volume_up", "volume_down", "next"]
    )
    app = ss.add_parser("spotify", help="act like the Spotify app (SPEC v0.9 §8.1)")
    app.add_argument("action", choices=["play", "pause", "volume"])
    app.add_argument("value", type=int, nargs="?", default=0)
    hold = ss.add_parser("hold", help="hold buttons, e.g. volume_up volume_down --seconds 5")
    hold.add_argument("buttons", nargs="+")
    hold.add_argument("--seconds", type=float, default=5.5)
    p.set_defaults(func=_cmd_sim)

    p = sub.add_parser("doctor", help="self-check: hardware, audio, network, server")
    p.add_argument("--offline", action="store_true", help="image build: no hardware, no network")
    p.set_defaults(func=_cmd_doctor)

    p = sub.add_parser("setupd", help="setup mode service (root, systemd)")
    p.add_argument("--host", default="10.42.0.1")
    p.add_argument("--port", type=int, default=80)
    p.set_defaults(func=_cmd_setupd)

    sub.add_parser("update", help="install a software update (root, systemd)").set_defaults(
        func=_cmd_update
    )

    p = sub.add_parser("soloist", help="Spotify Soloist (systemd user units)")
    so = p.add_subparsers(dest="soloist_command", required=True)
    so.add_parser("update", help="install or update Soloist").add_argument(
        "--force", action="store_true", help="install the current build now"
    )
    so.add_parser("exec", help="replace this process with Soloist (unit ExecStart)")
    so.add_parser("stopped", help="after Soloist ended (unit ExecStopPost)")
    so.add_parser("key", help="store the API key, read from stdin").add_argument(
        "--clear", action="store_true", help="remove the stored key"
    )
    p.set_defaults(func=_cmd_soloist)

    p = sub.add_parser("library", help="local library without server")
    ls = p.add_subparsers(dest="library_command", required=True)
    add = ls.add_parser("add", help="bind a folder of audio files to a figure")
    add.add_argument("uid")
    add.add_argument("path")
    add.add_argument("--label")
    add.add_argument("--title")
    add.set_defaults(func=_cmd_library_add)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    updates: dict[str, Any] = {}
    if args.data_dir is not None:
        updates["data_dir"] = args.data_dir
    if getattr(args, "sim", False):
        updates["sim"] = True
    if getattr(args, "server_url", None):
        updates["default_server_url"] = args.server_url
    if updates:
        settings = settings.model_copy(update=updates)
    fmt = "console" if args.command in {"run"} and settings.sim else settings.log_format
    configure_logging(settings.log_level, fmt)
    func: Command = args.func
    return func(args, settings)


if __name__ == "__main__":
    sys.exit(main())
