"""``myboxi-setupd``: the root service behind setup mode (SPEC §9.3).

Scan → access point ``Myboxi-NNNN`` → portal → connect → tell the agent (server URL,
announcements) → done. Ends after 15 minutes without activity.
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Protocol

from myboxi_agent.control import request
from myboxi_agent.core.model import Prompt, digit_prompt
from myboxi_agent.setup.nm import HOTSPOT_ADDRESS, Network
from myboxi_agent.setup.portal import Portal, Submission

log = logging.getLogger(__name__)

INACTIVITY_S = 15 * 60


class SetupNetwork(Protocol):
    def scan(self) -> list[Network]: ...
    def start_hotspot(self, ssid: str) -> bool: ...
    def stop_hotspot(self) -> None: ...
    def connect_wifi(self, ssid: str, password: str) -> bool: ...


class AgentLink(Protocol):
    async def announce(self, *prompts: str) -> None: ...

    async def set_server_url(self, url: str) -> None: ...

    async def set_soloist_key(self, key: str | None) -> None: ...


class SocketAgentLink:
    """Talks to the running agent through its control socket (root may open it)."""

    def __init__(self, socket: Path) -> None:
        self.socket = socket

    async def _send(self, payload: dict[str, object]) -> None:
        try:
            await request(self.socket, payload, limit_s=10)
        except (OSError, TimeoutError, ValueError):
            log.warning("agent not reachable", extra={"cmd": payload.get("cmd")})

    async def announce(self, *prompts: str) -> None:
        await self._send({"cmd": "announce", "prompts": [str(p) for p in prompts]})

    async def set_server_url(self, url: str) -> None:
        await self._send({"cmd": "set_server_url", "url": url})

    async def set_soloist_key(self, key: str | None) -> None:
        """SPEC v0.9 §9.3: the key goes straight to the agent's ``secret`` table."""
        if key is None:
            await self._send({"cmd": "clear_soloist_key"})
        else:
            await self._send({"cmd": "set_soloist_key", "key": key})


def spoken_name(ssid: str) -> list[str]:
    return [digit_prompt(d) for d in ssid if d.isdigit()]


async def run_setup(
    nm: SetupNetwork,
    agent: AgentLink,
    *,
    ssid: str,
    default_server_url: str,
    host: str = HOTSPOT_ADDRESS,
    port: int = 80,
    inactivity_s: float = INACTIVITY_S,
    soloist_key_set: bool = False,
) -> bool:
    networks = await asyncio.to_thread(nm.scan)  # before the radio becomes an access point
    error: str | None = None
    server_url = default_server_url
    while True:
        if not await asyncio.to_thread(nm.start_hotspot, ssid):
            await agent.announce(Prompt.SETUP_FAILED)
            return False
        await agent.announce(Prompt.SETUP_START, *spoken_name(ssid))
        portal = Portal(networks, server_url, error, soloist_key_set)
        server = await portal.serve(host, port)
        try:
            submission = await _wait(portal, inactivity_s)
        finally:
            await asyncio.sleep(1.0)  # let the "connecting" page reach the phone
            server.close()
        await asyncio.to_thread(nm.stop_hotspot)
        if submission is None:
            await agent.announce(Prompt.SETUP_END)
            return False
        if submission.soloist_clear or submission.soloist_key is not None:
            await agent.set_soloist_key(submission.soloist_key)
            soloist_key_set = submission.soloist_key is not None
        if await asyncio.to_thread(nm.connect_wifi, submission.ssid, submission.password):
            await agent.set_server_url(submission.server_url)
            await agent.announce(Prompt.SETUP_CONNECTED)
            log.info("setup finished", extra={"ssid": submission.ssid})
            return True
        await agent.announce(Prompt.SETUP_FAILED)
        error = "Die Verbindung hat nicht geklappt. Bitte WLAN und Passwort prüfen."
        server_url = submission.server_url
        networks = await asyncio.to_thread(nm.scan)


async def _wait(portal: Portal, inactivity_s: float) -> Submission | None:
    if portal.submission is None:
        raise RuntimeError("portal not serving")
    while True:
        remaining = inactivity_s - (time.monotonic() - portal.last_activity)
        if remaining <= 0:
            return None
        done, _ = await asyncio.wait({portal.submission}, timeout=min(remaining, 1.0))
        if done:
            return portal.submission.result()
