"""Dependencies for cookie-authenticated routes (web UI and the claim API)."""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from box_server.auth import csrf
from box_server.auth.sessions import SessionInfo, load_session
from box_server.db import get_db
from box_server.domain.authz import Perm, TenantContext
from box_server.domain.members import tenant_context
from box_server.settings import Settings

DbSession = Annotated[AsyncSession, Depends(get_db)]


def get_settings_dep(request: Request) -> Settings:
    settings: Settings = request.app.state.settings
    return settings


SettingsDep = Annotated[Settings, Depends(get_settings_dep)]


class LoginRequiredError(Exception):
    """Raised for anonymous access to protected routes; handled in app.py."""


async def get_session_info(
    request: Request, db: DbSession, settings: SettingsDep
) -> SessionInfo | None:
    cached: SessionInfo | None = getattr(request.state, "session_info", None)
    if cached is not None:
        return cached
    info = await load_session(db, request.cookies.get(settings.session_cookie_name))
    request.state.session_info = info
    return info


OptionalSession = Annotated[SessionInfo | None, Depends(get_session_info)]


async def require_session(info: OptionalSession) -> SessionInfo:
    if info is None:
        raise LoginRequiredError
    return info


CurrentSession = Annotated[SessionInfo, Depends(require_session)]


async def csrf_protect(request: Request, info: OptionalSession, settings: SettingsDep) -> None:
    """Router-level dependency: every unsafe request needs a valid CSRF token."""
    if request.method in csrf.SAFE_METHODS:
        return
    expected = info.csrf_token if info else request.cookies.get(csrf.ANON_COOKIE)
    try:
        await csrf.verify(request, expected, settings.base_url)
    except csrf.CsrfError as exc:
        raise HTTPException(status_code=403, detail="CSRF-Prüfung fehlgeschlagen.") from exc


def require(perm: Perm) -> Callable[..., Awaitable[TenantContext]]:
    """Resolve ``{tid}`` for the signed-in user.

    404 if the user is not a member (does not reveal whether the tenant exists), 403 if the
    role is insufficient (SPEC §3.2).
    """

    async def dependency(tid: uuid.UUID, info: CurrentSession, db: DbSession) -> TenantContext:
        ctx = await tenant_context(db, tid, info.user.id)
        if ctx is None:
            raise HTTPException(status_code=404)
        if not ctx.can(perm):
            raise HTTPException(status_code=403)
        return ctx

    return dependency


def client_ip(request: Request, settings: Settings) -> str:
    if settings.trust_proxy_headers:
        real = request.headers.get("x-real-ip")
        if real:
            return real
    return request.client.host if request.client else "unknown"
