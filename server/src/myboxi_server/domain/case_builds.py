"""Print files and previews for "Box gestalten" (hardware/case, docs/gehaeuse.md).

A build takes tens of milliseconds of CPU: at most two run at a time in worker threads, and the
results stay in a small in-process LRU cache keyed by the configuration digest.
"""

from __future__ import annotations

import gzip
from collections import OrderedDict
from collections.abc import Callable
from typing import Literal

import anyio

from myboxi_case import export
from myboxi_case.build import build
from myboxi_case.config import CaseConfig
from myboxi_case.layout import LayoutError

Kind = Literal["preview", "bundle"]
Make = Callable[[], bytes]


class CaseBuilds:
    def __init__(self, cache_bytes: int, concurrency: int = 2) -> None:
        self.cache_bytes = cache_bytes
        self._cache: OrderedDict[tuple[Kind, str], bytes | LayoutError] = OrderedDict()
        self._size = 0
        self._limiter = anyio.CapacityLimiter(concurrency)

    def cached(self, kind: Kind, cfg: CaseConfig) -> bool:
        return (kind, cfg.digest()) in self._cache

    async def preview(self, cfg: CaseConfig) -> bytes:
        """The gzip-compressed preview mesh; raises LayoutError."""
        return await self._get(
            "preview", cfg, lambda: gzip.compress(export.preview(build(cfg)), 6, mtime=0)
        )

    async def bundle(self, cfg: CaseConfig, url: str) -> bytes:
        """The ZIP with 3MF, STL and instructions; raises LayoutError."""
        return await self._get("bundle", cfg, lambda: export.bundle_zip(build(cfg), url))

    async def _get(self, kind: Kind, cfg: CaseConfig, make: Make) -> bytes:
        key = (kind, cfg.digest())
        hit = self._cache.get(key)
        if hit is None:
            try:
                hit = await anyio.to_thread.run_sync(make, limiter=self._limiter)
            except LayoutError as exc:
                hit = exc
            self._store(key, hit)
        else:
            self._cache.move_to_end(key)
        if isinstance(hit, LayoutError):
            raise hit
        return hit

    def _store(self, key: tuple[Kind, str], value: bytes | LayoutError) -> None:
        size = len(value) if isinstance(value, bytes) else 0
        if size > self.cache_bytes:
            return
        self._cache[key] = value
        self._size += size
        while self._size > self.cache_bytes:
            _, old = self._cache.popitem(last=False)
            self._size -= len(old) if isinstance(old, bytes) else 0
