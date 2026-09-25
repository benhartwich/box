from __future__ import annotations

import datetime as dt
import hashlib
from functools import cache
from pathlib import Path
from typing import Any, cast
from zoneinfo import ZoneInfo

from fastapi.templating import Jinja2Templates

from myboxi_server.domain.authz import Perm
from myboxi_server.models.enums import Role

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=TEMPLATE_DIR)
_globals = cast(dict[str, Any], templates.env.globals)
_globals["Perm"] = Perm
_globals["ROLE_LABELS"] = {
    Role.OWNER: "Besitzer",
    Role.ADMIN: "Admin",
    Role.CONTRIBUTOR: "Mitwirkend",
    Role.VIEWER: "Nur lesen",
}


@cache
def _static_digest(path: str) -> str:
    return hashlib.sha256((STATIC_DIR / path).read_bytes()).hexdigest()[:10]


def asset(path: str) -> str:
    """URL of a static file with its content hash: browsers never keep an old copy after an
    update (without it they may reuse a cached stylesheet for hours)."""
    return f"/static/{path}?v={_static_digest(path)}"


_globals["asset"] = asset

# Times in the UI are shown in the household's usual zone (SPEC default timezone).
UI_ZONE = ZoneInfo("Europe/Vienna")


def localtime(value: dt.datetime | None) -> str:
    if value is None:
        return "–"
    return value.astimezone(UI_ZONE).strftime("%d.%m.%Y %H:%M")


def duration(ms: int | None) -> str:
    if ms is None:
        return "–"
    seconds = round(ms / 1000)
    h, rest = divmod(seconds, 3600)
    m, s = divmod(rest, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def filesize(n: int | None) -> str:
    if n is None:
        return "–"
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}".replace(".", ",")
        size /= 1024
    return str(n)


_filters = cast(dict[str, Any], templates.env.filters)
_filters["localtime"] = localtime
_filters["duration"] = duration
_filters["filesize"] = filesize
