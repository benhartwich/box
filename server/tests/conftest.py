"""Shared fixtures: a migrated PostgreSQL test database and an app client.

Requires BOX_SERVER_TEST_DATABASE_URL (see tools/dev-postgres.sh and server/README.md).
"""

from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from pathlib import Path

import httpx
import pytest
from alembic import command
from alembic.config import Config
from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from box_server.app import create_app
from box_server.models import Base
from box_server.settings import Settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
TEST_JWT_KEY = "test-device-jwt-key-0123456789abcdef"


def _test_db_url() -> str:
    url = os.environ.get("BOX_SERVER_TEST_DATABASE_URL")
    if not url:
        pytest.exit("BOX_SERVER_TEST_DATABASE_URL is not set (source .dev/env)", returncode=2)
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
