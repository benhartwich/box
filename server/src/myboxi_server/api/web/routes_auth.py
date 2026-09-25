"""Login, logout, invitation acceptance and tenant selection."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import RedirectResponse, Response

from myboxi_server.api.web.deps import (
    CurrentSession,
    DbSession,
    OptionalSession,
    SettingsDep,
    client_ip,
    csrf_protect,
)
from myboxi_server.api.web.redirects import local_path
from myboxi_server.api.web.render import render
from myboxi_server.auth import ratelimit
from myboxi_server.auth.sessions import ABSOLUTE_LIFETIME, create_session, delete_session
from myboxi_server.domain import members
from myboxi_server.domain.errors import DomainError
from myboxi_server.settings import Settings

router = APIRouter(dependencies=[Depends(csrf_protect)])


def _safe_next(target: str | None) -> str:
    """Only allow local redirects."""
    return local_path(target) or "/"


def _set_session_cookie(response: Response, settings: Settings, token: str) -> None:
    response.set_cookie(
        settings.session_cookie_name,
        token,
        max_age=int(ABSOLUTE_LIFETIME.total_seconds()),
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


@router.get("/login")
async def login_form(request: Request, session: OptionalSession, next: str = "/") -> Response:
    if session is not None:
        return RedirectResponse(_safe_next(next), status_code=303)
    return render(request, "login.html", {"next": _safe_next(next)})


@router.post("/login")
async def login(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    email: Annotated[str, Form(max_length=254)],
    password: Annotated[str, Form(max_length=1024)],
    next: Annotated[str, Form()] = "/",
) -> Response:
    engine = request.app.state.engine
    try:
        await ratelimit.hit(
            engine, f"login:ip:{client_ip(request, settings)}", ratelimit.LOGIN_PER_IP
        )
        await ratelimit.hit(
            engine, f"login:acct:{email.strip().lower()}", ratelimit.LOGIN_PER_ACCOUNT
        )
    except ratelimit.RateLimitedError as exc:
        resp = render(
            request,
            "login.html",
            {"error": "Zu viele Anmeldeversuche. Bitte später erneut versuchen.", "email": email},
            status_code=429,
        )
        resp.headers["Retry-After"] = str(exc.retry_after)
        return resp
    user = await members.authenticate(db, email, password)
    if user is None:
        return render(
            request,
            "login.html",
            {"error": "E-Mail-Adresse oder Passwort ist falsch.", "email": email, "next": next},
            status_code=400,
        )
    old = request.cookies.get(settings.session_cookie_name)
    if old:
        await delete_session(db, old)
    token = await create_session(db, user.id)
    await db.commit()
    response = RedirectResponse(_safe_next(next), status_code=303)
    _set_session_cookie(response, settings, token)
    return response


@router.post("/logout")
async def logout(request: Request, db: DbSession, settings: SettingsDep) -> Response:
    token = request.cookies.get(settings.session_cookie_name)
    if token:
        await delete_session(db, token)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(settings.session_cookie_name, path="/")
    return response


@router.get("/")
async def index(request: Request, db: DbSession, session: CurrentSession) -> Response:
    memberships = await members.memberships_for_user(db, session.user.id)
    if len(memberships) == 1:
        return RedirectResponse(f"/t/{memberships[0].tenant.id}/", status_code=303)
    return render(request, "tenants.html", {"memberships": memberships}, session=session)


@router.get("/households")
async def households(request: Request, db: DbSession, session: CurrentSession) -> Response:
    memberships = await members.memberships_for_user(db, session.user.id)
    return render(request, "tenants.html", {"memberships": memberships}, session=session)


@router.post("/households")
async def create_household(
    request: Request,
    db: DbSession,
    session: CurrentSession,
    name: Annotated[str, Form(max_length=200)],
) -> Response:
    """SPEC v0.6 §3.2; then straight into the setup wizard."""
    try:
        await ratelimit.hit(
            request.app.state.engine,
            f"tenant-create:{session.user.id}",
            ratelimit.TENANT_CREATE_PER_USER,
        )
        tenant = await members.create_tenant(db, session.user.id, name)
    except ratelimit.RateLimitedError:
        error, status = "Zu viele neue Haushalte. Bitte später noch einmal versuchen.", 429
    except DomainError as exc:
        error, status = exc.message, 400
    else:
        await db.commit()
        return RedirectResponse(f"/t/{tenant.id}/setup", status_code=303)
    memberships = await members.memberships_for_user(db, session.user.id)
    return render(
        request,
        "tenants.html",
        {"memberships": memberships, "error": error, "name": name},
        session=session,
        status_code=status,
    )


# Invitation token travels in the query string: never logged (app and nginx log paths only).
@router.get("/invite")
async def invitation_form(
    request: Request, db: DbSession, session: OptionalSession, token: str = ""
) -> Response:
    found = await members.open_invitation(db, token) if token else None
    return render(
        request,
        "invite.html",
        {"invitation": found, "token": token},
        session=session,
        status_code=200 if found else 404,
    )


@router.post("/invite")
async def accept_invitation(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    session: OptionalSession,
    token: Annotated[str, Form()],
    display_name: Annotated[str, Form(max_length=100)] = "",
    password: Annotated[str | None, Form(max_length=1024)] = None,
) -> Response:
    try:
        user, tenant_id = await members.accept_invitation(
            db,
            token,
            current_user=session.user if session else None,
            display_name=display_name,
            password=password,
        )
    except DomainError as exc:
        found = await members.open_invitation(db, token)
        return render(
            request,
            "invite.html",
            {"invitation": found, "token": token, "error": exc.message},
            session=session,
            status_code=400,
        )
    new_token: str | None = None
    if session is None:
        new_token = await create_session(db, user.id)
    await db.commit()
    response = RedirectResponse(f"/t/{tenant_id}/", status_code=303)
    if new_token:
        _set_session_cookie(response, settings, new_token)
    return response
