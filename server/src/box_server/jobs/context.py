"""Database access for jobs: one engine per worker process."""

from __future__ import annotations

from functools import lru_cache

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from box_server.db import create_engine, create_sessionmaker
from box_server.settings import Settings, get_settings


@lru_cache(maxsize=1)
def job_sessionmaker() -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(create_engine(get_settings()))


def job_settings() -> Settings:
    return get_settings()
