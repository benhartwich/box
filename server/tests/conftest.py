"""Shared fixtures: a migrated PostgreSQL test database and an app client.

Requires MYBOXI_SERVER_TEST_DATABASE_URL (see tools/dev-postgres.sh and server/README.md).
"""

from __future__ import annotations

import asyncio
import os
import socket
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
import uvicorn
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from myboxi_server.app import create_app
from myboxi_server.models import Base
from myboxi_server.settings import Settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
TEST_JWT_KEY = "test-device-jwt-key-0123456789abcdef"


def _test_db_url() -> str:
    url = os.environ.get("MYBOXI_SERVER_TEST_DATABASE_URL")
    if not url:
        pytest.exit("MYBOXI_SERVER_TEST_DATABASE_URL is not set (source .dev/env)", returncode=2)
    return url


@pytest.fixture(scope="session")
def settings(tmp_path_factory: pytest.TempPathFactory) -> Settings:
    return Settings(
        env="dev",
        database_url=_test_db_url(),
        device_jwt_key=TEST_JWT_KEY,  # pyright: ignore[reportArgumentType]
        data_dir=tmp_path_factory.mktemp("data"),
        session_cookie_secure=False,
        log_format="console",
        update_manifest_url=None,  # no network in tests
    )


async def _reset_schema(url: str) -> None:
    engine = create_async_engine(url)
    async with engine.begin() as conn:
        await conn.execute(text("DROP SCHEMA public CASCADE"))
        await conn.execute(text("CREATE SCHEMA public"))
    await engine.dispose()


@pytest.fixture(scope="session")
def migrated_db(settings: Settings) -> str:
    """Fresh schema per test session, migrated to head via Alembic."""
    asyncio.run(_reset_schema(settings.async_database_url))
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = settings.async_database_url
    command.upgrade(cfg, "head")
    return settings.async_database_url


@pytest.fixture(scope="session")
async def engine(migrated_db: str) -> AsyncIterator[AsyncEngine]:
    eng = create_async_engine(migrated_db)
    yield eng
    await eng.dispose()


@pytest.fixture(autouse=True)
async def _clean_tables(request: pytest.FixtureRequest) -> AsyncIterator[None]:
    """Truncate all application tables after each test that touched the database."""
    yield
    if "engine" not in request.fixturenames:
        return
    eng: AsyncEngine = request.getfixturevalue("engine")
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    if tables:
        async with eng.begin() as conn:
            await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))


@pytest.fixture
async def app(settings: Settings, engine: AsyncEngine) -> AsyncIterator[FastAPI]:
    application = create_app(settings)
    async with application.router.lifespan_context(application):
        yield application


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[httpx.AsyncClient]:
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as c:
        yield c


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@pytest.fixture
async def live_server(settings: Settings, engine: AsyncEngine) -> AsyncIterator[str]:
    port = _free_port()
    config = uvicorn.Config(
        create_app(settings), host="127.0.0.1", port=port, log_config=None, access_log=False
    )
    server = uvicorn.Server(config)
    task = asyncio.create_task(server.serve())
    for _ in range(100):
        if server.started:
            break
        await asyncio.sleep(0.05)
    assert server.started
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await task
