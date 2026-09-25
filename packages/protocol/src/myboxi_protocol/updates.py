"""Update channel manifest (SPEC v0.7 §11.1), signed with Ed25519 next to the JSON file."""

from __future__ import annotations

import re
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, StringConstraints

from myboxi_protocol.common import HttpUrlStr, ProtocolModel, Sha256Hex

Version = Annotated[str, StringConstraints(pattern=r"^\d+\.\d+\.\d+$", max_length=32)]
_VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


class Bundle(ProtocolModel):
    url: HttpUrlStr
    sha256: Sha256Hex
    size: Annotated[int, Field(gt=0, le=2**31)]


class UpdateManifest(ProtocolModel):
    channel: Literal["stable"] = "stable"
    version: Version
    released_at: AwareDatetime
    bundle: Bundle


def version_key(version: str) -> tuple[int, int, int]:
    """Orders ``major.minor.patch``; anything else sorts first (never an upgrade)."""
    m = _VERSION.match(version)
    if m is None:
        return (-1, -1, -1)
    return (int(m[1]), int(m[2]), int(m[3]))


def is_newer(candidate: str, current: str) -> bool:
    return version_key(candidate) > version_key(current)
