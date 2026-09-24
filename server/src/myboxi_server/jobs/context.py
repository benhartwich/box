"""Settings, database and storage for jobs: configured once per worker process.

``myboxi-server worker`` calls ``configure(get_settings())``; tests configure their own settings
so jobs never touch a database other than the test database.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from myboxi_server.db import create_engine, create_sessionmaker
from myboxi_server.settings import Settings, get_settings
from myboxi_server.storage.filesystem import FilesystemAssetStore


@dataclass
class _JobContext:
    settings: Settings
    engine: AsyncEngine
    sessionmaker: async_sessionmaker[AsyncSession]
    store: FilesystemAssetStore


_ctx: _JobContext | None = None


def configure(settings: Settings) -> None:
    global _ctx
    engine = create_engine(settings)
    _ctx = _JobContext(
        settings=settings,
        engine=engine,
        sessionmaker=create_sessionmaker(engine),
        store=FilesystemAssetStore(settings.asset_dir, settings.accel_redirect_prefix),
    )


def _current() -> _JobContext:
    if _ctx is None:
        configure(get_settings())
    if _ctx is None:  # pragma: no cover - configure always sets it
        raise RuntimeError("job context not configured")
    return _ctx


def job_settings() -> Settings:
    return _current().settings


def job_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return _current().sessionmaker


def job_store() -> FilesystemAssetStore:
    return _current().store


async def dispose() -> None:
    global _ctx
    if _ctx is not None:
        await _ctx.engine.dispose()
        _ctx = None
