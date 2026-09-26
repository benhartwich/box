"""The own CA of a self-hosted server (SPEC v0.13 §9.3, §10).

Someone who runs their own server without a public certificate (e.g. only at home) gives
the box their CA certificate in the setup portal. It is trusted in addition to the usual
CAs, and only for the connections to that server: the device API (§7) and MQTT (§6). Podcast
feeds, Soloist and software updates never use it.
"""

from __future__ import annotations

import ssl

import certifi

MAX_CA_PEM = 8192
MAX_CERTS = 3
BEGIN = "-----BEGIN CERTIFICATE-----"


def normalize_ca(pem: str) -> str:
    return pem.replace("\r\n", "\n").strip() + "\n"


def valid_ca(pem: object) -> bool:
    """One to three PEM certificates that OpenSSL accepts as trust anchors."""
    if not isinstance(pem, str) or len(pem) > MAX_CA_PEM:
        return False
    text = normalize_ca(pem)
    if not text.startswith(BEGIN) or not 1 <= text.count(BEGIN) <= MAX_CERTS:
        return False
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    try:
        context.load_verify_locations(cadata=text)
    except (ssl.SSLError, ValueError):
        return False
    return True


def server_verify(ca_pem: str | None) -> ssl.SSLContext | bool:
    """``verify`` for httpx: the usual CAs (certifi, like httpx itself) plus the own CA."""
    if not ca_pem:
        return True
    context = ssl.create_default_context(cafile=certifi.where())
    context.load_verify_locations(cadata=ca_pem)
    return context
