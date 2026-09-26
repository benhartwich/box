"""MQTT topics (SPEC §6): everything of a box lives under ``myboxi/v1/{device_id}/``."""

from __future__ import annotations

import uuid
from typing import Final, Literal

PREFIX: Final = "myboxi/v1"
Leaf = Literal["notify", "cmd", "cmd/ack", "reported", "events", "online"]
# Direction per leaf: the server sends notify and cmd, the box everything else (SPEC §6.1-§6.6).
TO_BOX: Final[tuple[Leaf, ...]] = ("notify", "cmd")
FROM_BOX: Final[tuple[Leaf, ...]] = ("cmd/ack", "reported", "events", "online")
ONLINE: Final = b"1"
OFFLINE: Final = b"0"


def topic(device_id: uuid.UUID | str, leaf: Leaf) -> str:
    return f"{PREFIX}/{device_id}/{leaf}"


def parse(name: str) -> tuple[uuid.UUID, str] | None:
    """``myboxi/v1/<uuid>/<leaf>`` → (device id, leaf); None for anything else."""
    parts = name.split("/", 3)
    if len(parts) != 4 or f"{parts[0]}/{parts[1]}" != PREFIX:
        return None
    try:
        device_id = uuid.UUID(parts[2])
    except ValueError:
        return None
    return device_id, parts[3]
