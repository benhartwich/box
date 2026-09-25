"""Connections to public addresses only (SPEC v0.8 §8.2).

Feed and enclosure URLs come from third parties. The check runs where the TCP connection is
opened, after DNS resolution, for every connection including redirects; the socket then goes
to exactly the checked address (no second lookup, so DNS rebinding cannot slip through).
TLS still verifies the host name.
"""

from __future__ import annotations

import asyncio
import ipaddress
import socket
from collections.abc import Iterable
from typing import Any

import httpcore
import httpx

IpAddress = ipaddress.IPv4Address | ipaddress.IPv6Address


class BlockedAddress(httpcore.ConnectError):
    """The host resolves only to addresses the box must not reach."""


def is_public(ip: IpAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped is not None:
        ip = ip.ipv4_mapped
    return ip.is_global and not ip.is_multicast


async def resolve(host: str, port: int) -> list[IpAddress]:
    try:
        return [ipaddress.ip_address(host.strip("[]"))]
    except ValueError:
        pass
    infos = await asyncio.get_running_loop().getaddrinfo(host, port, type=socket.SOCK_STREAM)
    addresses: list[IpAddress] = []
    for info in infos:
        address = ipaddress.ip_address(str(info[4][0]).split("%")[0])
        if address not in addresses:
            addresses.append(address)
    return addresses


class PublicOnlyBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, inner: httpcore.AsyncNetworkBackend) -> None:
        self.inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's interface
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        try:
            addresses = await asyncio.wait_for(resolve(host, port), timeout)
        except (OSError, TimeoutError) as exc:
            raise httpcore.ConnectError(f"cannot resolve {host}") from exc
        public = [a for a in addresses if is_public(a)]
        if not public:
            raise BlockedAddress(f"{host} is not a public address")
        error: Exception = BlockedAddress(f"{host}: no address")
        for address in public:
            try:
                return await self.inner.connect_tcp(
                    str(address), port, timeout, local_address, socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                error = exc
        raise error

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,  # noqa: ASYNC109 - httpcore's interface
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise BlockedAddress("unix sockets are not allowed")

    async def sleep(self, seconds: float) -> None:
        await self.inner.sleep(seconds)


def public_transport() -> httpx.AsyncHTTPTransport:
    """An httpx transport whose connections pass ``PublicOnlyBackend``.

    httpx has no public hook for the network backend; the pool's backend attribute is
    replaced. A test checks that a loopback host is refused, so an httpx upgrade that moves
    the attribute fails loudly instead of silently allowing everything.
    """
    transport = httpx.AsyncHTTPTransport(retries=0)
    pool = getattr(transport, "_pool")  # noqa: B009
    inner = getattr(pool, "_network_backend")  # noqa: B009
    if not isinstance(inner, httpcore.AsyncNetworkBackend):
        raise RuntimeError("unexpected httpx internals")
    setattr(pool, "_network_backend", PublicOnlyBackend(inner))  # noqa: B010
    return transport


def feed_client(*, allow_private: bool = False) -> httpx.AsyncClient:
    """HTTP client for feeds and enclosures: ≤ 5 redirects, public addresses only unless
    ``allow_private`` (development against a local feed)."""
    return httpx.AsyncClient(
        transport=None if allow_private else public_transport(),
        follow_redirects=True,
        max_redirects=5,
        timeout=httpx.Timeout(30.0, connect=15.0, read=60.0),
        headers={"User-Agent": "Myboxi (+https://myboxi.eu)"},
    )
