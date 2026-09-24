"""Template rendering with the common context (user, CSRF token, tenant)."""

from __future__ import annotations

from typing import Any

from fastapi import Request
from fastapi.responses import HTMLResponse

from myboxi_server.api.web.templating import templates
from myboxi_server.auth import csrf
from myboxi_server.auth.sessions import SessionInfo
from myboxi_server.domain.authz import TenantContext


def render(
    request: Request,
    template: str,
    context: dict[str, Any] | None = None,
    *,
    session: SessionInfo | None = None,
    ctx: TenantContext | None = None,
    status_code: int = 200,
) -> HTMLResponse:
    """Render for a signed-in user (or anonymously with a double-submit CSRF cookie)."""
    anon_token: str | None = None
    if session is None:
        anon_token = request.cookies.get(csrf.ANON_COOKIE) or csrf.new_anon_token()
    data: dict[str, Any] = {
        "session": session,
        "user": session.user if session else None,
        "csrf_token": session.csrf_token if session else anon_token,
        "ctx": ctx,
        "tid": ctx.tenant_id if ctx else None,
    }
    data.update(context or {})
    response = templates.TemplateResponse(request, template, data, status_code=status_code)
    if anon_token and request.cookies.get(csrf.ANON_COOKIE) != anon_token:
        settings = request.app.state.settings
        response.set_cookie(
            csrf.ANON_COOKIE,
            anon_token,
            httponly=True,
            secure=settings.session_cookie_secure,
            samesite="strict",
            path="/",
        )
    return response
