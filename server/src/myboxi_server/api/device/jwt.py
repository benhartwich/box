"""Short-lived device JWTs (SPEC §7.2): HS256, 1 h, key from configuration."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import jwt

AUDIENCE = "myboxi-device"
ALGORITHM = "HS256"


@dataclass(frozen=True)
class DeviceClaims:
    device_id: uuid.UUID
    tenant_id: uuid.UUID
    generation: int


def issue(key: str, claims: DeviceClaims, ttl_s: int) -> str:
    now = dt.datetime.now(dt.UTC)
    payload = {
        "sub": str(claims.device_id),
        "tid": str(claims.tenant_id),
        "gen": claims.generation,
        "aud": AUDIENCE,
        "iat": now,
        "exp": now + dt.timedelta(seconds=ttl_s),
    }
    return jwt.encode(payload, key, algorithm=ALGORITHM)  # pyright: ignore[reportUnknownMemberType]


class InvalidTokenError(Exception):
    pass


def verify(key: str, token: str) -> DeviceClaims:
    try:
        payload = jwt.decode(  # pyright: ignore[reportUnknownMemberType]
            token,
            key,
            algorithms=[ALGORITHM],
            audience=AUDIENCE,
            options={"require": ["sub", "tid", "gen", "exp", "aud"]},
        )
        return DeviceClaims(
            device_id=uuid.UUID(str(payload["sub"])),
            tenant_id=uuid.UUID(str(payload["tid"])),
            generation=int(payload["gen"]),
        )
    except (jwt.InvalidTokenError, ValueError, KeyError, TypeError) as exc:
        raise InvalidTokenError from exc
