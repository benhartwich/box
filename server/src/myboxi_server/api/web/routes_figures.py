"""Figures, unknown figures and bindings (SPEC §3.5, §3.9)."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from myboxi_server.api.web.deps import (
    BindingWriteCtx,
    CurrentSession,
    DbSession,
    ReadCtx,
    TokenWriteCtx,
    csrf_protect,
)
from myboxi_server.api.web.redirects import tenant_path
from myboxi_server.api.web.render import render
from myboxi_server.auth.sessions import SessionInfo
from myboxi_server.domain import bindings, contents, tokens
from myboxi_server.domain.authz import TenantContext
from myboxi_server.domain.errors import DomainError, NotFoundError
from myboxi_server.models.enums import RepeatMode

router = APIRouter(prefix="/t/{tid}/figures", dependencies=[Depends(csrf_protect)])

REPEAT_LABELS = {
    RepeatMode.OFF: "Aus",
    RepeatMode.ALL: "Alles wiederholen",
    RepeatMode.ONE: "Titel wiederholen",
}


async def _list_page(
    request: Request,
    db: DbSession,
    session: SessionInfo,
    ctx: TenantContext,
    *,
    error: str | None = None,
    form: dict[str, str] | None = None,
    status_code: int = 200,
) -> Response:
    content_list = await contents.list_contents(db, ctx)
    return render(
        request,
        "figures.html",
        {
            "tokens": await tokens.list_tokens(db, ctx),
            "unknown": await tokens.unknown_tokens(db, ctx),
            "bindings": await bindings.bindings_by_token(db, ctx),
            "content_titles": {c.id: c.title for c in content_list},
            "error": error,
            "form": form or {},
        },
        session=session,
        ctx=ctx,
        status_code=status_code,
    )


@router.get("")
async def figures(
    request: Request, db: DbSession, session: CurrentSession, ctx: ReadCtx
) -> Response:
    return await _list_page(request, db, session, ctx)


@router.post("")
async def create_figure(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: TokenWriteCtx,
    uid: Annotated[str, Form(max_length=64)],
    label: Annotated[str, Form(max_length=200)],
) -> Response:
    try:
        token = await tokens.create_token(db, ctx, uid=uid, label=label)
    except DomainError as exc:
        await db.rollback()
        return await _list_page(
            request,
            db,
            session,
            ctx,
            error=exc.message,
            form={"uid": uid, "label": label},
            status_code=400,
        )
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/figures/{token.id}", status_code=303)


@router.post("/adopt")
async def adopt_unknown(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: TokenWriteCtx,
    uid: Annotated[str, Form(max_length=64)],
    label: Annotated[str, Form(max_length=200)] = "Neue Figur",
    next: Annotated[str | None, Form()] = None,
) -> Response:
    """One click: an unknown UID reported by a box becomes a figure (SPEC §2)."""
    try:
        token = await tokens.create_token(db, ctx, uid=uid, label=label.strip() or "Neue Figur")
    except DomainError as exc:
        await db.rollback()
        return await _list_page(request, db, session, ctx, error=exc.message, status_code=400)
    await db.commit()
    target = tenant_path(ctx.tenant_id, next) or f"/t/{ctx.tenant_id}/figures/{token.id}"
    return RedirectResponse(target, status_code=303)


async def _figure_page(
    request: Request,
    db: DbSession,
    session: SessionInfo,
    ctx: TenantContext,
    token_id: uuid.UUID,
    *,
    error: str | None = None,
    notice: str | None = None,
    status_code: int = 200,
) -> Response:
    token = await tokens.get_token(db, ctx, token_id)
    return render(
        request,
        "figure.html",
        {
            "token": token,
            "binding": await bindings.get_binding(db, ctx, token_id),
            "contents": await contents.list_contents(db, ctx),
            "repeat_labels": REPEAT_LABELS,
            "error": error,
            "notice": notice,
        },
        session=session,
        ctx=ctx,
        status_code=status_code,
    )


@router.get("/{token_id}")
async def figure(
    request: Request, db: DbSession, session: CurrentSession, ctx: ReadCtx, token_id: uuid.UUID
) -> Response:
    return await _figure_page(request, db, session, ctx, token_id)


@router.post("/{token_id}")
async def update_figure(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: TokenWriteCtx,
    token_id: uuid.UUID,
    label: Annotated[str, Form(max_length=200)],
    icon: Annotated[str, Form(max_length=16)] = "",
) -> Response:
    try:
        await tokens.update_token(db, ctx, token_id, label=label, icon=icon)
    except NotFoundError:
        raise
    except DomainError as exc:
        await db.rollback()
        return await _figure_page(
            request, db, session, ctx, token_id, error=exc.message, status_code=400
        )
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/figures/{token_id}", status_code=303)


@router.post("/{token_id}/delete")
async def delete_figure(db: DbSession, ctx: TokenWriteCtx, token_id: uuid.UUID) -> Response:
    await tokens.delete_token(db, ctx, token_id)
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/figures", status_code=303)


@router.post("/{token_id}/binding")
async def set_binding(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    ctx: BindingWriteCtx,
    token_id: uuid.UUID,
    content_id: Annotated[uuid.UUID, Form()],
    repeat: Annotated[RepeatMode, Form()] = RepeatMode.OFF,
    resume: Annotated[bool, Form()] = False,
    shuffle: Annotated[bool, Form()] = False,
    next: Annotated[str | None, Form()] = None,
) -> Response:
    await bindings.set_binding(
        db, ctx, token_id, content_id=content_id, resume=resume, shuffle=shuffle, repeat=repeat
    )
    await db.commit()
    if target := tenant_path(ctx.tenant_id, next):
        return RedirectResponse(target, status_code=303)
    return await _figure_page(request, db, session, ctx, token_id, notice="Zuordnung gespeichert.")


@router.post("/{token_id}/binding/delete")
async def delete_binding(db: DbSession, ctx: BindingWriteCtx, token_id: uuid.UUID) -> Response:
    await bindings.delete_binding(db, ctx, token_id)
    await db.commit()
    return RedirectResponse(f"/t/{ctx.tenant_id}/figures/{token_id}", status_code=303)
