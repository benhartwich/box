"""Fixed-window rate limits in PostgreSQL (no Redis; SPEC §7.1, §7.2).

Counts are committed in their own transaction, so rejected and failed attempts count too.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine


@dataclass(frozen=True)
class Limit:
    count: int
    window_s: int


class RateLimitedError(Exception):
    def __init__(self, retry_after: int) -> None:
        super().__init__(f"rate limited, retry after {retry_after}s")
        self.retry_after = retry_after


_HIT = text(
    """
    WITH w AS (
        SELECT to_timestamp(floor(extract(epoch FROM now()) / :win) * :win) AS start
    )
    INSERT INTO rate_limit (key, window_start, count)
    SELECT :key, w.start, 1 FROM w
    ON CONFLICT (key, window_start) DO UPDATE SET count = rate_limit.count + 1
    RETURNING count, extract(epoch FROM (window_start + make_interval(secs => :win) - now()))
    """
)


async def hit(engine: AsyncEngine, key: str, limits: Sequence[Limit]) -> None:
    """Count one attempt for ``key``; raise ``RateLimitedError`` if any limit is exceeded."""
    retry_after = 0
    async with engine.begin() as conn:
        for limit in limits:
            row = (
                await conn.execute(_HIT, {"key": f"{key}|{limit.window_s}", "win": limit.window_s})
            ).one()
            count, remaining = int(row[0]), float(row[1])
            if count > limit.count:
                retry_after = max(retry_after, math.ceil(remaining))
    if retry_after:
        raise RateLimitedError(max(retry_after, 1))


# Limits from SPEC v0.3 §7.1/§7.2 and the plan (login).
CLAIM_PER_USER = (Limit(5, 60), Limit(20, 3600))
CLAIM_PER_TENANT = (Limit(30, 3600),)
PAIRING_START_PER_IP = (Limit(10, 3600),)
POLL_PER_TOKEN = (Limit(1, 2),)
DEVICE_TOKEN_PER_DEVICE = (Limit(10, 60),)
LOGIN_PER_ACCOUNT = (Limit(10, 900),)
LOGIN_PER_IP = (Limit(50, 900),)
TENANT_CREATE_PER_USER = (Limit(5, 3600),)  # SPEC v0.6 §3.2
# "Box gestalten": only builds that miss the cache count (docs/gehaeuse.md).
CASE_BUILD_PER_IP = (Limit(60, 600), Limit(400, 86400))
CASE_REQUEST_PER_IP = (Limit(5, 3600),)
CASE_REQUEST_PER_EMAIL = (Limit(3, 86400),)
