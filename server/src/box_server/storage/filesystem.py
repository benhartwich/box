"""Filesystem backend. In production nginx delivers files via ``X-Accel-Redirect`` from an
``internal`` location (CLAUDE.md: never without a prior permission check)."""

from __future__ import annotations

import asyncio
import os
import shutil
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, Response

CACHE_CONTROL = "private, max-age=31536000, immutable"


class FilesystemAssetStore:
    def __init__(self, root: Path, accel_prefix: str | None = None) -> None:
        self.root = root
        self.accel_prefix = accel_prefix.rstrip("/") + "/" if accel_prefix else None

    def path(self, rel: str) -> Path:
        path = (self.root / rel).resolve()
        if not path.is_relative_to(self.root.resolve()):
            raise ValueError("path escapes the asset store")
        return path

    async def put(self, src: Path, rel: str) -> None:
        await asyncio.to_thread(self._put, src, self.path(rel))

    @staticmethod
    def _put(src: Path, dest: Path) -> None:
        if dest.exists():
            src.unlink(missing_ok=True)
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        with src.open("rb") as fh:
            os.fsync(fh.fileno())
        try:
            src.replace(dest)  # atomic on the same filesystem
        except OSError:
            tmp = dest.with_suffix(dest.suffix + ".part")
            shutil.copyfile(src, tmp)
            tmp.replace(dest)
            src.unlink(missing_ok=True)
        dest.chmod(0o640)
        dir_fd = os.open(dest.parent, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)

    async def exists(self, rel: str) -> bool:
        return await asyncio.to_thread(self.path(rel).is_file)

    async def delete(self, rel: str) -> None:
        await asyncio.to_thread(self.path(rel).unlink, True)

    def response(self, request: Request, rel: str, *, etag: str, media_type: str) -> Response:
        quoted = f'"{etag}"'
        headers = {"ETag": quoted, "Cache-Control": CACHE_CONTROL}
        inm = request.headers.get("if-none-match")
        if inm and quoted in {t.strip() for t in inm.split(",")}:
            return Response(status_code=304, headers=headers)
        if self.accel_prefix:
            # nginx serves the file (Range, sendfile) from its internal location.
            headers["X-Accel-Redirect"] = self.accel_prefix + rel
            return Response(status_code=200, headers=headers, media_type=media_type)
        return FileResponse(self.path(rel), media_type=media_type, headers=headers)
