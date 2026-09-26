"""Command line interface ``myboxi-server``."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

from myboxi_server.logconfig import configure_logging
from myboxi_server.settings import get_settings

ALEMBIC_INI = Path(__file__).resolve().parents[2] / "alembic.ini"


def _cmd_dev(args: argparse.Namespace) -> int:
    import uvicorn

    os.environ.setdefault("MYBOXI_SERVER_ENV", "dev")
    # CSRF compares the Origin header with base_url; match what the browser will use.
    os.environ.setdefault("MYBOXI_SERVER_BASE_URL", f"http://{args.host}:{args.port}")
    settings = get_settings()
    configure_logging(settings.log_level, "console")
    uvicorn.run(
        "myboxi_server.app:create_app",
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
        "myboxi_server.app:create_app",
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
    from myboxi_server.jobs import context
    from myboxi_server.jobs.app import open_job_app

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


def _cmd_mqtt(args: argparse.Namespace) -> int:
    """SPEC §6 (M2): the MQTT service (systemd unit myboxi-server-mqtt)."""
    from myboxi_server.db import create_engine, create_sessionmaker
    from myboxi_server.mqtt.service import MqttService

    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    if not settings.mqtt_enabled:
        print("MQTT is off: set MYBOXI_SERVER_MQTT_HOST and _MQTT_PASSWORD.", file=sys.stderr)
        return 2

    async def run() -> None:
        engine = create_engine(settings)
        try:
            await MqttService(settings, create_sessionmaker(engine)).run()
        finally:
            await engine.dispose()

    asyncio.run(run())
    return 0


def _cmd_mqtt_setup(args: argparse.Namespace) -> int:
    """Once per broker: the server's account may use ``myboxi/v1/#``."""
    from myboxi_server.mqtt.service import setup_server_role

    settings = get_settings()
    configure_logging(settings.log_level, "console")
    if not settings.mqtt_enabled:
        print("MQTT is off: set MYBOXI_SERVER_MQTT_HOST and _MQTT_PASSWORD.", file=sys.stderr)
        return 2
    asyncio.run(setup_server_role(settings))
    print("MQTT server role is set up.")
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    from alembic import command
    from alembic.config import Config

    settings = get_settings()
    configure_logging(settings.log_level, "console")
    cfg = Config(str(Path(os.environ.get("MYBOXI_SERVER_ALEMBIC_INI", ALEMBIC_INI))))
    command.upgrade(cfg, args.revision)
    return 0


def _cmd_create_admin(args: argparse.Namespace) -> int:
    """First owner (SPEC §3.2): creates a tenant owned by a new or existing user."""
    import getpass

    from myboxi_server.db import create_engine, create_sessionmaker
    from myboxi_server.domain.errors import DomainError
    from myboxi_server.domain.members import create_tenant_with_owner, user_by_email

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
                print(f"Mandant {tenant.name} ({tenant.id}) mit Besitzer {user.email} angelegt.")
                return 0
        finally:
            await engine.dispose()

    return asyncio.run(run())


def _cmd_case_requests(args: argparse.Namespace) -> int:
    """Order requests for printed cases (docs/gehaeuse.md): list, show, set status, delete."""
    import uuid

    from myboxi_case.config import CaseConfig
    from myboxi_server.db import create_engine, create_sessionmaker
    from myboxi_server.domain import case_requests
    from myboxi_server.domain.errors import NotFoundError
    from myboxi_server.models import CaseRequest
    from myboxi_server.models.enums import CaseRequestStatus

    settings = get_settings()
    configure_logging(settings.log_level, "console")

    async def run() -> int:
        engine = create_engine(settings)
        try:
            async with create_sessionmaker(engine)() as db:
                match args.action:
                    case "list":
                        status = CaseRequestStatus(args.status) if args.status else None
                        for req in await case_requests.list_requests(db, status):
                            cfg = CaseConfig.model_validate(req.config)
                            print(
                                f"{req.id}  {req.created_at:%Y-%m-%d}  {req.status.value:<11} "
                                f"{req.quantity}x {cfg.form:<5} {cfg.name!r:<16} {req.email}"
                            )
                    case "show":
                        req = await db.get(CaseRequest, uuid.UUID(args.id))
                        if req is None:
                            raise NotFoundError()
                        cfg = CaseConfig.model_validate(req.config)
                        print(f"{req.contact_name} <{req.email}>, {req.country}")
                        print(f"Status {req.status.value}, {req.quantity} Stück")
                        for label, value in case_requests.describe(cfg):
                            print(f"  {label}: {value}")
                        print(req.message or "(keine Nachricht)")
                    case "status":
                        await case_requests.set_status(
                            db, uuid.UUID(args.id), CaseRequestStatus(args.status)
                        )
                        await db.commit()
                    case _:
                        await case_requests.delete_request(db, uuid.UUID(args.id))
                        await db.commit()
        except NotFoundError:
            print("Anfrage nicht gefunden.", file=sys.stderr)
            return 1
        finally:
            await engine.dispose()
        return 0

    return asyncio.run(run())


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="myboxi-server")
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

    sub.add_parser("mqtt", help="MQTT service: box accounts, notify, commands").set_defaults(
        func=_cmd_mqtt
    )
    sub.add_parser(
        "mqtt-setup", help="give the server account its broker role (once)"
    ).set_defaults(func=_cmd_mqtt_setup)

    p = sub.add_parser("migrate", help="apply database migrations")
    p.add_argument("revision", nargs="?", default="head")
    p.set_defaults(func=_cmd_migrate)

    p = sub.add_parser("create-admin", help="create a tenant and its owner")
    p.add_argument("--email", required=True)
    p.add_argument("--tenant-name", required=True)
    p.add_argument("--display-name")
    p.add_argument("--password-stdin", action="store_true", help="read the password from stdin")
    p.set_defaults(func=_cmd_create_admin)

    p = sub.add_parser("case-requests", help="order requests for printed cases")
    actions = p.add_subparsers(dest="action", required=True)
    statuses = ["unconfirmed", "confirmed", "answered", "done", "cancelled"]
    a = actions.add_parser("list")
    a.add_argument("--status", choices=statuses)
    a = actions.add_parser("show")
    a.add_argument("id")
    a = actions.add_parser("status")
    a.add_argument("id")
    a.add_argument("status", choices=statuses)
    a = actions.add_parser("delete")
    a.add_argument("id")
    p.set_defaults(func=_cmd_case_requests)

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
