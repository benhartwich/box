"""Asset store interface. The filesystem backend is the default; S3 can follow later."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Protocol

from starlette.requests import Request
from starlette.responses import Response

SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def relpath_for(sha256: str, ext: str) -> str:
    """SPEC §3.8: ``ab/cd/<sha256>.<ext>``."""
    if not SHA256_RE.match(sha256):
        raise ValueError("invalid sha256")
    if not re.fullmatch(r"[a-z0-9]{1,8}", ext):
        raise ValueError("invalid extension")
    return f"{sha256[:2]}/{sha256[2:4]}/{sha256}.{ext}"


class AssetStore(Protocol):
    async def put(self, src: Path, rel: str) -> None:
        """Move ``src`` into the store at ``rel`` (atomic, idempotent)."""
        ...

    async def exists(self, rel: str) -> bool: ...

    async def delete(self, rel: str) -> None: ...

    def response(self, request: Request, rel: str, *, etag: str, media_type: str) -> Response:
        """HTTP response delivering the asset (Range requests supported)."""
        ...
