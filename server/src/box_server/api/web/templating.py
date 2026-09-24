from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fastapi.templating import Jinja2Templates

from box_server.domain.authz import Perm
from box_server.models.enums import Role

TEMPLATE_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"

templates = Jinja2Templates(directory=TEMPLATE_DIR)
_globals = cast(dict[str, Any], templates.env.globals)
_globals["Perm"] = Perm
_globals["ROLE_LABELS"] = {
    Role.OWNER: "Owner",
    Role.ADMIN: "Admin",
    Role.CONTRIBUTOR: "Mitwirkend",
    Role.VIEWER: "Nur lesen",
}
