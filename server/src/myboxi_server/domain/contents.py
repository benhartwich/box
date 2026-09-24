"""Contents and collection items (SPEC §3.6, §3.7).

``source`` is validated with the protocol models, so what the state endpoint sends is exactly
what was stored.
"""

from __future__ import annotations

import re
import uuid
from typing import Any

from pydantic import ValidationError
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_protocol.state import PodcastSource, SpotifySource, StreamSource
from myboxi_server.domain.authz import Perm, TenantContext
from myboxi_server.domain.errors import InvalidInputError, NotFoundError
from myboxi_server.models import Asset, Content, ContentItem
from myboxi_server.models.enums import ContentKind

MAX_TITLE = 200
_SPOTIFY_URL = re.compile(
    r"^https://open\.spotify\.com/(?:intl-[a-z]{2}/)?"
    r"(album|playlist|track|show|episode|artist)/([0-9A-Za-z]{22})(?:[/?#].*)?$"
)


def clean_title(raw: str) -> str:
    title = raw.strip()
    if not 1 <= len(title) <= MAX_TITLE:
        raise InvalidInputError("Bitte einen Titel mit höchstens 200 Zeichen angeben.")
    return title


def spotify_source(raw: str) -> dict[str, Any]:
    """Accepts ``spotify:album:…`` or an open.spotify.com link (SPEC §3.6: only the URI)."""
    value = raw.strip()
    if m := _SPOTIFY_URL.match(value):
        value = f"spotify:{m.group(1)}:{m.group(2)}"
    try:
        return SpotifySource(uri=value).model_dump(mode="json")
    except ValidationError:
        raise InvalidInputError("Bitte eine Spotify-URI wie spotify:album:… angeben.") from None


def podcast_source(feed_url: str, keep_latest: int, order: str) -> dict[str, Any]:
    try:
        return PodcastSource.model_validate(
            {"feed_url": feed_url.strip(), "keep_latest": keep_latest, "order": order}
        ).model_dump(mode="json")
    except ValidationError:
        raise InvalidInputError(
            "Bitte eine gültige Feed-URL (http/https) und 1–100 Folgen angeben."
        ) from None


def stream_source(url: str) -> dict[str, Any]:
    try:
        return StreamSource(url=url.strip()).model_dump(mode="json")
    except ValidationError:
        raise InvalidInputError("Bitte eine gültige Stream-URL angeben.") from None


async def list_contents(db: AsyncSession, ctx: TenantContext) -> list[Content]:
    ctx.require(Perm.READ)
    return list(
        await db.scalars(
            select(Content)
            .where(Content.tenant_id == ctx.tenant_id)
            .order_by(func.lower(Content.title))
        )
    )


async def get_content(
    db: AsyncSession, ctx: TenantContext, content_id: uuid.UUID, *, for_update: bool = False
) -> Content:
    ctx.require(Perm.READ)
    stmt = select(Content).where(Content.id == content_id, Content.tenant_id == ctx.tenant_id)
    if for_update:
        stmt = stmt.with_for_update()
    content = await db.scalar(stmt)
    if content is None:
        raise NotFoundError()
    return content


async def create_content(
    db: AsyncSession, ctx: TenantContext, *, kind: ContentKind, title: str, source: dict[str, Any]
) -> Content:
    ctx.require(Perm.CONTENT_WRITE)
    content = Content(tenant_id=ctx.tenant_id, kind=kind, title=clean_title(title), source=source)
    db.add(content)
    await db.flush()
    return content


async def update_content(
    db: AsyncSession,
    ctx: TenantContext,
    content_id: uuid.UUID,
    *,
    title: str,
    source: dict[str, Any] | None = None,
) -> Content:
    ctx.require(Perm.CONTENT_WRITE)
    content = await get_content(db, ctx, content_id, for_update=True)
    content.title = clean_title(title)
    if source is not None and content.kind != ContentKind.COLLECTION:
        content.source = source
    await db.flush()
    return content


async def delete_content(db: AsyncSession, ctx: TenantContext, content_id: uuid.UUID) -> None:
    """Deletes items, uploads and bindings with it (FK cascades); assets are collected later."""
    ctx.require(Perm.CONTENT_WRITE)
    content = await get_content(db, ctx, content_id)
    await db.delete(content)
    await db.flush()


async def list_items(
    db: AsyncSession, ctx: TenantContext, content_id: uuid.UUID
) -> list[tuple[ContentItem, Asset]]:
    ctx.require(Perm.READ)
    rows = await db.execute(
        select(ContentItem, Asset)
        .join(
            Asset, (Asset.id == ContentItem.asset_id) & (Asset.tenant_id == ContentItem.tenant_id)
        )
        .where(ContentItem.tenant_id == ctx.tenant_id, ContentItem.content_id == content_id)
        .order_by(ContentItem.position)
    )
    return list(rows.tuples())


async def _item(
    db: AsyncSession, ctx: TenantContext, content_id: uuid.UUID, item_id: uuid.UUID
) -> ContentItem:
    await get_content(db, ctx, content_id, for_update=True)  # serializes item changes
    item = await db.scalar(
        select(ContentItem).where(
            ContentItem.id == item_id,
            ContentItem.content_id == content_id,
            ContentItem.tenant_id == ctx.tenant_id,
        )
    )
    if item is None:
        raise NotFoundError()
    return item


async def move_item(
    db: AsyncSession, ctx: TenantContext, content_id: uuid.UUID, item_id: uuid.UUID, delta: int
) -> None:
    """Swap with the neighbour above (-1) or below (+1)."""
    ctx.require(Perm.CONTENT_WRITE)
    if delta not in (-1, 1):
        raise InvalidInputError("Ungültige Richtung.")
    item = await _item(db, ctx, content_id, item_id)
    neighbour = await db.scalar(
        select(ContentItem).where(
            ContentItem.content_id == content_id, ContentItem.position == item.position + delta
        )
    )
    if neighbour is None:
        return
    item.position, neighbour.position = neighbour.position, item.position
    await db.flush()


async def rename_item(
    db: AsyncSession, ctx: TenantContext, content_id: uuid.UUID, item_id: uuid.UUID, title: str
) -> None:
    ctx.require(Perm.CONTENT_WRITE)
    item = await _item(db, ctx, content_id, item_id)
    item.title = clean_title(title)
    await db.flush()


async def delete_item(
    db: AsyncSession, ctx: TenantContext, content_id: uuid.UUID, item_id: uuid.UUID
) -> None:
    """Remove a track and close the gap in the positions."""
    ctx.require(Perm.CONTENT_WRITE)
    item = await _item(db, ctx, content_id, item_id)
    position = item.position
    await db.delete(item)
    await db.flush()
    await db.execute(
        update(ContentItem)
        .where(ContentItem.content_id == content_id, ContentItem.position > position)
        .values(position=ContentItem.position - 1)
    )
    await db.flush()
