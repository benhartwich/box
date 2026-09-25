"""Local redirect targets only (no open redirects)."""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit


def local_path(target: str | None) -> str | None:
    if not target or not target.startswith("/") or target.startswith("//") or "\\" in target:
        return None
    parts = urlsplit(target)
    return None if parts.scheme or parts.netloc else target


def tenant_path(tid: uuid.UUID, target: str | None) -> str | None:
    """A path inside the same household, e.g. back into the setup wizard."""
    path = local_path(target)
    return path if path is not None and path.startswith(f"/t/{tid}/") else None
