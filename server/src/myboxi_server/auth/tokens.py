"""Random tokens that are stored only as SHA-256 hashes (sessions, poll tokens, invitations)."""

from __future__ import annotations

import hashlib
import secrets


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> bytes:
    return hashlib.sha256(token.encode()).digest()
