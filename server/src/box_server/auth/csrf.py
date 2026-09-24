"""CSRF protection for cookie-authenticated requests.

* Signed-in users: synchronizer token stored in the session row, sent as form field
  ``csrf_token`` or header ``X-CSRF-Token`` (HTMX sets it via ``hx-headers``).
* Anonymous forms (login, invitation): double-submit cookie ``box_csrf``.
* Additionally, a present ``Origin`` header must match the server's base URL.
"""

from __future__ import annotations

import hmac
import secrets
from urllib.parse import urlsplit

from starlette.requests import Request

ANON_COOKIE = "box_csrf"
FIELD = "csrf_token"
HEADER = "X-CSRF-Token"
SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


class CsrfError(Exception):
    pass


def new_anon_token() -> str:
    return secrets.token_urlsafe(32)


def _origin_ok(request: Request, base_url: str) -> bool:
    origin = request.headers.get("origin")
    if origin is None or origin == "null":
        return origin is None
    expected = urlsplit(base_url)
    got = urlsplit(origin)
    return (got.scheme, got.netloc) == (expected.scheme, expected.netloc)


async def submitted_token(request: Request) -> str | None:
    header = request.headers.get(HEADER)
    if header:
        return header
    content_type = request.headers.get("content-type", "")
    if content_type.startswith(("application/x-www-form-urlencoded", "multipart/form-data")):
        form = await request.form()
        value = form.get(FIELD)
        return value if isinstance(value, str) else None
    return None


async def verify(request: Request, expected: str | None, base_url: str) -> None:
    """Raise ``CsrfError`` unless the unsafe request carries the expected token."""
    if request.method in SAFE_METHODS:
        return
    if not _origin_ok(request, base_url):
        raise CsrfError("origin mismatch")
    got = await submitted_token(request)
    if not expected or not got or not hmac.compare_digest(expected, got):
        raise CsrfError("csrf token mismatch")
