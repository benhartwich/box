"""Secrets the server must read again later (a household's Spotify refresh token), sealed
with AES-GCM. The key is derived from the server secret, so a database dump alone is not
enough to use them. Rotating ``device_jwt_key`` means reconnecting Spotify."""

from __future__ import annotations

import hashlib
import hmac
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from myboxi_server.settings import Settings

NONCE = 12


def _key(settings: Settings, purpose: str) -> bytes:
    secret = settings.device_jwt_key.get_secret_value().encode()
    return hmac.new(secret, f"myboxi secretbox v1 {purpose}".encode(), hashlib.sha256).digest()


def seal(settings: Settings, purpose: str, plaintext: str) -> bytes:
    nonce = os.urandom(NONCE)
    return nonce + AESGCM(_key(settings, purpose)).encrypt(nonce, plaintext.encode(), None)


def unseal(settings: Settings, purpose: str, blob: bytes) -> str | None:
    """None if the blob was sealed with another key or changed."""
    try:
        data = AESGCM(_key(settings, purpose)).decrypt(blob[:NONCE], blob[NONCE:], None)
    except (InvalidTag, ValueError):
        return None
    return data.decode()
