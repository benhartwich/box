"""Local control socket (Unix domain, owner-only): status and simulation commands.

Never a network port (SPEC §9.3). One JSON object per line in both directions.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

Handler = Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]


class ControlServer:
    def __init__(self, path: Path, handler: Handler) -> None:
        self.path = path
        self.handler = handler

    async def serve(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(FileNotFoundError):
            self.path.unlink()
        old_umask = os.umask(0o177)
        try:
            server = await asyncio.start_unix_server(self._client, path=str(self.path))
        finally:
            os.umask(old_umask)
        async with server:
            await server.serve_forever()

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await reader.readline()
            try:
                request: dict[str, Any] = json.loads(line or b"{}")
                response = await self.handler(request)
            except (ValueError, KeyError, TypeError) as exc:
                response = {"ok": False, "error": str(exc)}
            writer.write(json.dumps(response, default=str).encode() + b"\n")
            await writer.drain()
        finally:
            writer.close()


async def request(path: Path, payload: dict[str, Any], limit_s: float = 30.0) -> dict[str, Any]:
    reader, writer = await asyncio.wait_for(asyncio.open_unix_connection(str(path)), limit_s)
    try:
        writer.write(json.dumps(payload).encode() + b"\n")
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), limit_s)
        result: dict[str, Any] = json.loads(line)
        return result
    finally:
        writer.close()
