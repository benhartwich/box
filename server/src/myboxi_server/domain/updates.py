"""Box software updates in words (SPEC v0.7 §11.1): what the box reports and the newest release.

The newest release comes from the channel manifest the boxes use. The server only shows it;
it neither verifies the signature (the boxes do) nor ever blocks a page on the network: a stale
value is refreshed in the background.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import math
import time
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from pydantic import ValidationError

from myboxi_protocol.reported import ReportedData
from myboxi_protocol.updates import UpdateManifest, is_newer

log = logging.getLogger(__name__)

REFRESH_S = 3600.0
MAX_MANIFEST_BYTES = 64 * 1024
BEHIND_NOTICE = dt.timedelta(hours=24)
FIRST_UPDATING_VERSION = "0.3.0"
CODES = {
    "bad_signature": "die Signatur des Updates war ungültig",
    "bad_manifest": "die Update-Beschreibung war fehlerhaft",
    "checksum": "der Download war beschädigt",
    "download": "der Download ist fehlgeschlagen",
    "no_space": "auf der SD-Karte ist zu wenig Platz",
    "bad_bundle": "das Update-Paket war beschädigt",
    "unhealthy": "die neue Version lief nicht einwandfrei",
}

Level = Literal["ok", "info", "warn", "fail"]


@dataclass(frozen=True)
class Release:
    version: str
    released_at: dt.datetime


def _fetch(url: str) -> bytes:
    request = urllib.request.Request(url, headers={"User-Agent": "myboxi-server"})  # noqa: S310
    with urllib.request.urlopen(request, timeout=10) as response:  # noqa: S310 - https URL from settings
        data: bytes = response.read(MAX_MANIFEST_BYTES + 1)
    return data


class UpdateChannel:
    def __init__(self, url: str | None, fetch: Callable[[str], bytes] = _fetch) -> None:
        self.url = url
        self.fetch = fetch
        self._latest: Release | None = None
        self._fetched_at = -math.inf
        self._task: asyncio.Task[None] | None = None

    def latest(self) -> Release | None:
        """What is known now; starts a refresh in the background when stale."""
        stale = time.monotonic() - self._fetched_at > REFRESH_S
        if self.url and stale and (self._task is None or self._task.done()):
            self._task = asyncio.get_running_loop().create_task(self.refresh())
        return self._latest

    async def refresh(self) -> None:
        if not self.url:
            return
        self._fetched_at = time.monotonic()
        try:
            data = await asyncio.to_thread(self.fetch, self.url)
            if len(data) > MAX_MANIFEST_BYTES:
                raise ValueError("manifest too large")
            manifest = UpdateManifest.model_validate_json(data)
        except (OSError, ValueError, ValidationError) as exc:
            log.info("update manifest not available", extra={"error": str(exc)})
            return
        self._latest = Release(manifest.version, manifest.released_at)


@dataclass(frozen=True)
class SoftwareView:
    version: str
    image: str | None
    level: Level
    text: str
    notice: bool  # worth a card on the household's home page


def software_view(
    data: ReportedData, latest: Release | None, now: dt.datetime | None = None
) -> SoftwareView:
    now = now or dt.datetime.now(dt.UTC)
    version, update = data.agent_version, data.update
    behind = latest is not None and is_newer(latest.version, version)
    long_behind = behind and latest is not None and now - latest.released_at > BEHIND_NOTICE

    def view(level: Level, text: str, notice: bool = False) -> SoftwareView:
        return SoftwareView(version, data.image_version, level, text, notice)

    if update is None:
        if behind and latest is not None and is_newer(FIRST_UPDATING_VERSION, version):
            return view(
                "warn",
                f"Version {latest.version} ist erschienen. Diese Box aktualisiert sich noch nicht "
                "selbst: Spiel einmal das neue Image auf die SD-Karte, danach kommen Updates "
                "automatisch.",
                long_behind,
            )
        return view("ok", "Aktuell.")
    target = update.version or (latest.version if latest else "")
    reason = CODES.get(update.code or "", update.code or "unbekannter Fehler")
    match update.state:
        case "installed" | "up_to_date" if not behind:
            return view("ok", "Aktuell.")
        case "available":
            return view(
                "info",
                f"Update auf {target} ist geladen. Automatische Updates sind aus: schalte sie "
                "unten in den Einstellungen ein, dann installiert die Box es selbst.",
            )
        case "downloading":
            return view("info", f"Update auf {target} wird geladen.")
        case "waiting":
            return view(
                "info",
                f"Update auf {target} ist geladen und wird installiert, sobald nichts mehr "
                "spielt. Das dauert nur ein paar Sekunden.",
            )
        case "failed":
            return view(
                "fail",
                f"Update fehlgeschlagen: {reason}. Die bisherige Version läuft weiter, die Box "
                "versucht es später erneut.",
                True,
            )
        case "rolled_back":
            return view(
                "fail",
                f"Version {target} lief auf dieser Box nicht einwandfrei. Die Box ist zur "
                "bisherigen Version zurückgekehrt und wartet auf die nächste.",
                True,
            )
        case _:  # behind: the box has not seen the newest release yet
            newest = latest.version if latest else target
            return view(
                "info",
                f"Version {newest} ist erschienen. Die Box holt sie, sobald sie online ist.",
                long_behind,
            )
