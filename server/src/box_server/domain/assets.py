"""Asset garbage collection (SPEC §3.8): assets no content refers to any more."""

from __future__ import annotations

import datetime as dt
import logging

from sqlalchemy import ColumnElement, and_, delete, exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from box_server.domain.uploads import lock_sha
from box_server.models import Asset, Content, ContentItem
from box_server.storage.base import AssetStore

log = logging.getLogger(__name__)

GRACE = dt.timedelta(hours=24)


def _unreferenced() -> ColumnElement[bool]:
    return and_(
        ~exists().where(ContentItem.asset_id == Asset.id, ContentItem.tenant_id == Asset.tenant_id),
        ~exists().where(Content.cover_asset_id == Asset.id, Content.tenant_id == Asset.tenant_id),
    )


async def collect_garbage(maker: async_sessionmaker[AsyncSession], store: AssetStore) -> int:
    """Delete unreferenced asset rows older than 24 h; delete a file once no tenant uses it."""
    async with maker() as db:
        candidates = (
            await db.execute(
                select(Asset.id, Asset.sha256).where(
                    Asset.created_at < func.now() - GRACE, _unreferenced()
                )
            )
        ).all()
    removed = 0
    for asset_id, sha in candidates:
        async with maker() as db:
            await lock_sha(db, sha)
            row = await db.scalar(select(Asset).where(Asset.id == asset_id, _unreferenced()))
            if row is None:
                continue
            path = row.storage_path
            await db.execute(delete(Asset).where(Asset.id == asset_id))
            still_used = await db.scalar(select(exists().where(Asset.storage_path == path)))
            if not still_used:
                await store.delete(path)
            await db.commit()
            removed += 1
    if removed:
        log.info("removed unreferenced assets", extra={"count": removed})
    return removed
