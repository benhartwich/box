"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from box_server.api.web import routes_auth, routes_members
from box_server.api.web.deps import LoginRequiredError
from box_server.api.web.render import render
from box_server.api.web.templating import STATIC_DIR
from box_server.db import create_engine, create_sessionmaker
from box_server.domain.authz import PermissionDeniedError
from box_server.domain.errors import NotFoundError
from box_server.jobs.app import connected_job_app
from box_server.settings import Settings, get_settings

access_log = logging.getLogger("box_server.access")

SECURITY_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"same-origin",
    b"content-security-policy": (
        b"default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
        b"frame-ancestors 'none'; base-uri 'self'; form-action 'self'"
    ),
}


class AccessLogMiddleware:
    """Logs method, path (never the query string, SPEC §7.1 poll_token), status and duration.

    Also adds security headers to every HTTP response.
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        start = time.perf_counter()
        status = 500

        async def send_wrapper(message: Message) -> None:
            nonlocal status
            if message["type"] == "http.response.start":
                status = message["status"]
                headers = list(message.get("headers", []))
                present = {k.lower() for k, _ in headers}
                headers.extend((k, v) for k, v in SECURITY_HEADERS.items() if k not in present)
                message["headers"] = headers
            await send(message)

        try:
            await self.app(scope, receive, send_wrapper)
        finally:
            access_log.info(
                "%s %s %s",
                scope["method"],
                scope["path"],
                status,
                extra={"duration_ms": round((time.perf_counter() - start) * 1000, 1)},
            )


def _wants_html(request: Request) -> bool:
    return not request.url.path.startswith("/api/")


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(LoginRequiredError)
    async def login_required(request: Request, exc: LoginRequiredError) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not _wants_html(request):
            return JSONResponse({"detail": "Nicht angemeldet."}, status_code=401)
        target = "/login?next=" + quote(request.url.path, safe="/")
        if request.headers.get("hx-request"):
            return Response(status_code=204, headers={"HX-Redirect": target})
        return RedirectResponse(target, status_code=303)

    @app.exception_handler(PermissionDeniedError)
    async def forbidden(request: Request, exc: PermissionDeniedError) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _http_error(request, HTTPException(status_code=403))

    @app.exception_handler(NotFoundError)
    async def not_found(request: Request, exc: NotFoundError) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _http_error(request, HTTPException(status_code=404))

    @app.exception_handler(HTTPException)
    async def http_error(request: Request, exc: HTTPException) -> Response:  # pyright: ignore[reportUnusedFunction]
        return await _http_error(request, exc)

    @app.exception_handler(RequestValidationError)
    async def invalid(request: Request, exc: RequestValidationError) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not _wants_html(request):
            return JSONResponse({"detail": "Ungültige Anfrage."}, status_code=422)
        return PlainTextResponse("Ungültige Eingabe.", status_code=422)


_MESSAGES = {
    403: "Dafür fehlt dir die Berechtigung.",
    404: "Nicht gefunden.",
}


async def _http_error(request: Request, exc: HTTPException) -> Response:
    headers = dict(exc.headers or {})
    if not _wants_html(request):
        return JSONResponse({"detail": exc.detail}, status_code=exc.status_code, headers=headers)
    message = _MESSAGES.get(exc.status_code, str(exc.detail))
    response = render(
        request,
        "error.html",
        {"message": message, "status": exc.status_code},
        status_code=exc.status_code,
    )
    response.headers.update(headers)
    return response


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        engine = create_engine(settings)
        app.state.engine = engine
        app.state.sessionmaker = create_sessionmaker(engine)
        job_app = connected_job_app(settings)
        await job_app.open_async()
        app.state.job_app = job_app
        try:
            yield
        finally:
            await job_app.close_async()
            await engine.dispose()

    app = FastAPI(
        title="Box Server",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.is_dev else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.is_dev else None,
    )
    app.state.settings = settings
    app.add_middleware(AccessLogMiddleware)
    _install_error_handlers(app)

    app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(routes_auth.router)
    app.include_router(routes_members.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        async with app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app
