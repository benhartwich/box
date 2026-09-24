"""Command line interface ``box-server``."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from box_server.logconfig import configure_logging
from box_server.settings import get_settings

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _cmd_dev(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ.setdefault("BOX_SERVER_ENV", "dev")
    settings = get_settings()
    configure_logging(settings.log_level, "console")
    uvicorn.run(
        "box_server.app:create_app",
        factory=True,
        host=args.host,
        port=args.port,
        reload=args.reload,
        access_log=False,
        log_config=None,
    )
    return 0


def _cmd_serve(args: argparse.Namespace) -> int:
    """Production server behind nginx: systemd socket activation (fd 3) or a Unix socket."""
    import uvicorn

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    listen: dict[str, object]
    if args.uds:
        listen = {"uds": args.uds}
    elif os.environ.get("LISTEN_FDS"):
        listen = {"fd": 3}
    else:
        print("serve needs --uds PATH or systemd socket activation", file=sys.stderr)
        return 2
    uvicorn.run(
        "box_server.app:create_app",
        factory=True,
        workers=args.workers,
        access_log=False,
        log_config=None,
        proxy_headers=False,
        server_header=False,
        **listen,  # pyright: ignore[reportArgumentType]
    )
    return 0


def _cmd_worker(args: argparse.Namespace) -> int:
    from box_server.jobs import context
    from box_server.jobs.app import open_job_app

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    context.configure(settings)
    settings.tmp_dir.mkdir(parents=True, exist_ok=True)

    async def run() -> None:
        try:
            async with open_job_app(settings) as app:
                await app.run_worker_async(
                    concurrency=args.concurrency or settings.worker_concurrency
                )
        finally:
            await context.dispose()

    asyncio.run(run())
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    configure_logging(settings.log_level, "console")
    cfg = Config(str(Path(os.environ.get("BOX_SERVER_ALEMBIC_INI", ALEMBIC_INI))))
    command.upgrade(cfg, args.revision)
    return 0


def _cmd_create_admin(args: argparse.Namespace) -> int:
    """First owner (SPEC §3.2): creates a tenant owned by a new or existing user."""
    import getpass

    from box_server.db import create_engine, create_sessionmaker
    from box_server.domain.errors import DomainError
    from box_server.domain.members import create_tenant_with_owner, user_by_email

    settings = get_settings()
    configure_logging(settings.log_level, "console")

    async def run() -> int:
        engine = create_engine(settings)
        try:
            async with create_sessionmaker(engine)() as db:
                password: str | None = None
                if await user_by_email(db, args.email) is None:
                    if args.password_stdin:
                        password = sys.stdin.readline().rstrip("\n")
                    else:
                        password = getpass.getpass("Passwort: ")
                        if password != getpass.getpass("Passwort wiederholen: "):
                            print("Passwörter stimmen nicht überein.", file=sys.stderr)
                            return 1
                try:
                    tenant, user = await create_tenant_with_owner(
                        db,
                        tenant_name=args.tenant_name,
                        email=args.email,
                        display_name=args.display_name or "",
                        password=password,
                    )
                except DomainError as exc:
                    print(exc.message, file=sys.stderr)
                    return 1
                await db.commit()
                print(f"Mandant {tenant.name} ({tenant.id}) mit Owner {user.email} angelegt.")
                return 0
        finally:
            await engine.dispose()

    return asyncio.run(run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="box-server")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("dev", help="development server on localhost (no nginx)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8000)
    p.add_argument("--no-reload", dest="reload", action="store_false")
    p.set_defaults(func=_cmd_dev)

    p = sub.add_parser("serve", help="production server (systemd socket or --uds)")
    p.add_argument("--uds", help="Unix socket path (instead of socket activation)")
    p.add_argument("--workers", type=int, default=1)
    p.set_defaults(func=_cmd_serve)

    p = sub.add_parser("worker", help="background job worker")
    p.add_argument("--concurrency", type=int, default=0)
    p.set_defaults(func=_cmd_worker)

    p = sub.add_parser("migrate", help="apply database migrations")
    p.add_argument("revision", nargs="?", default="head")
    p.set_defaults(func=_cmd_migrate)

    p = sub.add_parser("create-admin", help="create a tenant and its owner")
    p.add_argument("--email", required=True)
    p.add_argument("--tenant-name", required=True)
    p.add_argument("--display-name")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.set_defaults(func=_cmd_create_admin)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
