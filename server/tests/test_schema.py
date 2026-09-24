"""Schema checks: migrations match the models, round-trip, DB-level tenant isolation (SPEC §3)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.config import Config
from alembic.migration import MigrationContext
from sqlalchemy import insert, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from box_server.models import Asset, Base, Content, ContentItem, Tenant
from box_server.settings import Settings

ALEMBIC_INI = Path(__file__).resolve().parents[1] / "alembic.ini"
# Columns that exist only in migrations (maintained by triggers, not mapped in the ORM).
TRIGGER_ONLY_COLUMNS = {
    ("tenant", "config_rev_xact"),
    ("device", "device_rev_xact"),
    ("content", "rev_xact"),
}


def _include(obj: Any, name: str | None, type_: str, reflected: bool, compare_to: Any) -> bool:
    if type_ == "table" and name is not None and name.startswith("procrastinate"):
        return False
    if type_ == "column" and reflected and compare_to is None:
        return (obj.table.name, name) not in TRIGGER_ONLY_COLUMNS
    return True


async def test_models_match_migrations(engine: AsyncEngine) -> None:
    def diff(conn: Connection) -> list[Any]:
        ctx = MigrationContext.configure(
            conn, opts={"include_object": _include, "compare_type": True}
        )
        return compare_metadata(ctx, Base.metadata)

    async with engine.connect() as conn:
        changes = await conn.run_sync(diff)
    assert changes == []


@pytest.fixture
async def scratch_db(settings: Settings) -> AsyncIterator[str]:
    """A throwaway database next to the test database."""
    base, _, _ = settings.async_database_url.rpartition("/")
    name = f"box_mig_{uuid.uuid4().hex[:8]}"
    admin = create_async_engine(settings.async_database_url, isolation_level="AUTOCOMMIT")
    async with admin.connect() as conn:
        await conn.execute(text(f'CREATE DATABASE "{name}"'))
    try:
        yield f"{base}/{name}"
    finally:
        async with admin.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{name}" WITH (FORCE)'))
        await admin.dispose()


def test_migrations_downgrade_and_upgrade(scratch_db: str) -> None:
    cfg = Config(str(ALEMBIC_INI))
    cfg.attributes["database_url"] = scratch_db
    command.upgrade(cfg, "head")
    command.downgrade(cfg, "base")
    command.upgrade(cfg, "head")


async def test_composite_fk_blocks_cross_tenant_links(engine: AsyncEngine) -> None:
    """CLAUDE.md rule 7: the database refuses to link rows of different tenants."""
    t1, t2 = uuid.uuid4(), uuid.uuid4()
    asset_t2, content_t1 = uuid.uuid4(), uuid.uuid4()
    async with engine.begin() as conn:
        await conn.execute(insert(Tenant), [{"id": t1, "name": "A"}, {"id": t2, "name": "B"}])
        await conn.execute(
            insert(Asset).values(
                id=asset_t2,
                tenant_id=t2,
                sha256="b" * 64,
                mime="audio/ogg; codecs=opus",
                bytes=1,
                storage_path="bb/bb/x.opus",
            )
        )
        await conn.execute(
            insert(Content).values(id=content_t1, tenant_id=t1, kind="collection", title="x")
        )
    with pytest.raises(IntegrityError):
        async with engine.begin() as conn:
            await conn.execute(
                insert(ContentItem).values(
                    tenant_id=t1,
                    content_id=content_t1,
                    position=0,
                    asset_id=asset_t2,
                    title="x",
                    duration_ms=1,
                )
            )
