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


def _cmd_sim(args: argparse.Namespace, settings: Settings) -> int:
    match args.sim_command:
        case "place":
            payload: dict[str, Any] = {"cmd": "place", "uid": args.uid}
        case "remove" | "finish":
            payload = {"cmd": args.sim_command}
        case "press":
            payload = {"cmd": "press", "button": args.button}
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="myboxi-agent")
    parser.add_argument("--version", action="version", version=f"myboxi-agent {__version__}")
    parser.add_argument("--data-dir", type=Path, help="override MYBOXI_AGENT_DATA_DIR")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("version", help="print the version").set_defaults(func=_cmd_version)

    p = sub.add_parser("run", help="run the agent")
    p.add_argument("--sim", action="store_true", help="simulated hardware (CLAUDE.md rule 2)")
    p.set_defaults(func=_cmd_run)

    sub.add_parser("status", help="state of the running agent").set_defaults(func=_cmd_status)

    p = sub.add_parser("sim", help="drive a simulated agent (run --sim)")
    ss = p.add_subparsers(dest="sim_command", required=True)
    ss.add_parser("place", help="place a figure").add_argument("uid")
    ss.add_parser("remove", help="remove the figure")
    ss.add_parser("finish", help="let the playlist reach its end")
    ss.add_parser("press", help="press a button").add_argument(
        "button", choices=["play_pause", "volume_up", "volume_down", "next"]
    )
    hold = ss.add_parser("hold", help="hold buttons, e.g. volume_up volume_down --seconds 5")
    hold.add_argument("buttons", nargs="+")
    hold.add_argument("--seconds", type=float, default=5.5)
    p.set_defaults(func=_cmd_sim)

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
    if updates:
        settings = settings.model_copy(update=updates)
    fmt = "console" if args.command in {"run"} and settings.sim else settings.log_format
    configure_logging(settings.log_level, fmt)
    func: Command = args.func
    return func(args, settings)


if __name__ == "__main__":
    sys.exit(main())
