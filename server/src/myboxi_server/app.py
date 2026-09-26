"""FastAPI application factory."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from urllib.parse import quote

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from myboxi_server.api.device import claim as device_claim
from myboxi_server.api.device import router as device_router
from myboxi_server.api.device.errors import ApiError, code_for_status, error_response
from myboxi_server.api.web import (
    routes_auth,
    routes_boxes,
    routes_case,
    routes_contents,
    routes_figures,
    routes_members,
    routes_setup,
    routes_spotify,
)
from myboxi_server.api.web.deps import LoginRequiredError
from myboxi_server.api.web.render import render
from myboxi_server.api.web.templating import STATIC_DIR
from myboxi_server.db import create_engine, create_sessionmaker
from myboxi_server.domain.authz import PermissionDeniedError
from myboxi_server.domain.case_builds import CaseBuilds
from myboxi_server.domain.errors import NotFoundError
from myboxi_server.domain.spotify import SpotifyWeb
from myboxi_server.domain.updates import UpdateChannel
from myboxi_server.jobs.app import open_job_app
from myboxi_server.settings import Settings, get_settings
from myboxi_server.storage.filesystem import FilesystemAssetStore

access_log = logging.getLogger("myboxi_server.access")

SECURITY_HEADERS = {
    b"x-content-type-options": b"nosniff",
    b"x-frame-options": b"DENY",
    b"referrer-policy": b"same-origin",
    b"content-security-policy": (
        # Spotify (SPEC v0.10 §3.6): covers in search results, the login of the household app.
        b"default-src 'self'; img-src 'self' data: https://i.scdn.co https://*.spotifycdn.com; "
        b"style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'self'; "
        b"form-action 'self' https://accounts.spotify.com"
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


def _api_detail(detail: object, fallback: str) -> str:
    return detail if isinstance(detail, str) else fallback


class CachedStaticFiles(StaticFiles):
    """Pages link static files with ``?v=<content hash>`` (templating.asset) and third-party
    files under a versioned path: those never change. Everything else is revalidated."""

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        versioned = b"v=" in scope.get("query_string", b"") or path.startswith("vendor/")
        response.headers["Cache-Control"] = (
            "public, max-age=31536000, immutable" if versioned else "no-cache"
        )
        return response


def _install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(request: Request, exc: ApiError) -> Response:  # pyright: ignore[reportUnusedFunction]
        return error_response(exc.status_code, exc.code, exc.message, exc.headers)

    @app.exception_handler(LoginRequiredError)
    async def login_required(request: Request, exc: LoginRequiredError) -> Response:  # pyright: ignore[reportUnusedFunction]
        if not _wants_html(request):
            return error_response(401, code_for_status(401), "Not signed in")
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
            return error_response(400, code_for_status(400), "Invalid request")
        return PlainTextResponse("Ungültige Eingabe.", status_code=422)


_MESSAGES = {
    403: "Dafür fehlt dir die Berechtigung.",
    404: "Nicht gefunden.",
}


async def _http_error(request: Request, exc: HTTPException) -> Response:
    headers = dict(exc.headers or {})
    if not _wants_html(request):
        return error_response(
            exc.status_code,
            code_for_status(exc.status_code),
            _api_detail(exc.detail, "Error"),
            headers,
        )
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
        try:
            async with open_job_app(settings) as job_app:
                app.state.job_app = job_app
                yield
        finally:
            await engine.dispose()

    app = FastAPI(
        title="Myboxi Server",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/api/docs" if settings.is_dev else None,
        redoc_url=None,
        openapi_url="/api/openapi.json" if settings.is_dev else None,
    )
    app.state.settings = settings
    app.state.update_channel = UpdateChannel(settings.update_manifest_url)
    app.state.case_builds = CaseBuilds(settings.case_cache_mb * 1024 * 1024)
    app.state.asset_store = FilesystemAssetStore(settings.asset_dir, settings.accel_redirect_prefix)
    app.state.spotify = SpotifyWeb()
    app.add_middleware(AccessLogMiddleware)
    _install_error_handlers(app)

    app.mount("/static", CachedStaticFiles(directory=STATIC_DIR), name="static")
    app.include_router(routes_auth.router)
    app.include_router(routes_members.router)
    app.include_router(routes_boxes.router)
    app.include_router(routes_setup.router)
    app.include_router(routes_figures.router)
    app.include_router(routes_contents.router)
    app.include_router(routes_spotify.router)
    app.include_router(routes_case.router)
    app.include_router(device_router.router)
    app.include_router(device_claim.router)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> dict[str, str]:  # pyright: ignore[reportUnusedFunction]
        async with app.state.engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app
