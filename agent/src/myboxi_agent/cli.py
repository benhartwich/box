"""Command line interface ``myboxi-agent``."""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable

from myboxi_agent import __version__
from myboxi_agent.config import get_settings
from myboxi_agent.logconfig import configure_logging

Command = Callable[[argparse.Namespace], int]


def _cmd_version(args: argparse.Namespace) -> int:
    del args
    print(f"myboxi-agent {__version__}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="myboxi-agent")
    parser.add_argument("--version", action="version", version=f"myboxi-agent {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("version", help="print the version")
    p.set_defaults(func=_cmd_version)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    func: Command = args.func
    return func(args)


if __name__ == "__main__":
    sys.exit(main())
