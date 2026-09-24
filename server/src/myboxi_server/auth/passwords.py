"""Argon2id hashing for user passwords and device secrets (SPEC §10).

Hashing is CPU-bound (~50 ms), so async callers use the ``*_async`` variants that run in a
worker thread instead of blocking the event loop.
"""

from __future__ import annotations

import asyncio

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

_hasher = PasswordHasher()  # argon2-cffi defaults: Argon2id, t=3, m=64 MiB, p=4
# Verified against when a user does not exist, so timing does not reveal unknown accounts.
_DUMMY_HASH = _hasher.hash("dummy password for constant-time failures")


def hash_secret(secret: str) -> str:
    return _hasher.hash(secret)


def verify_secret(hashed: str | None, secret: str) -> bool:
    try:
        return _hasher.verify(hashed or _DUMMY_HASH, secret) and hashed is not None
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False


async def hash_secret_async(secret: str) -> str:
    return await asyncio.to_thread(hash_secret, secret)


async def verify_secret_async(hashed: str | None, secret: str) -> bool:
    return await asyncio.to_thread(verify_secret, hashed, secret)
