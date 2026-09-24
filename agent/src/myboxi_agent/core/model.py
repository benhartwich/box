"""Values the core works with. Sources are opaque strings (file paths) for the player."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from myboxi_protocol.state import RepeatMode


@dataclass(frozen=True)
class PlanItem:
    source: str
    title: str
    duration_ms: int


@dataclass(frozen=True)
class ResumePoint:
    item_index: int
    position_ms: int


@dataclass(frozen=True)
class Playable:
    token_id: uuid.UUID
    content_id: uuid.UUID
    items: tuple[PlanItem, ...]
    resume: bool
    shuffle: bool
    repeat: RepeatMode


@dataclass(frozen=True)
class Loading:
    """A new binding is still downloading and the figure had none before (SPEC §5.3)."""

    token_id: uuid.UUID


@dataclass(frozen=True)
class Unavailable:
    """The provider cannot play this content now (SPEC §9.1)."""

    token_id: uuid.UUID
    content_id: uuid.UUID
    provider: str
    code: str


@dataclass(frozen=True)
class Unknown:
    uid: str


Resolution = Playable | Loading | Unavailable | Unknown


class Action(StrEnum):
    """Button actions (SPEC §9.4)."""

    PLAY_PAUSE = "play_pause"
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    NEXT = "next"
    SETUP_MODE = "setup_mode"
    REPAIR = "repair"


class Prompt(StrEnum):
    """Audible states (SPEC §1.7). Files come from agent/prompts.toml."""

    TONE_START = "tone_start"
    TONE_ERROR = "tone_error"
    TONE_ATTENTION = "tone_attention"
    UNKNOWN_TOKEN = "unknown_token"
    LOADING = "loading"
    UNAVAILABLE = "unavailable"
    QUIET_TIME = "quiet_time"
    PAIRING_INTRO = "pairing_intro"
    PAIRING_DONE = "pairing_done"
    REPAIR = "repair"
    SETUP_START = "setup_start"
    SETUP_CONNECTED = "setup_connected"
    SETUP_FAILED = "setup_failed"
    SETUP_END = "setup_end"
    HELLO = "hello"


def digit_prompt(d: str) -> str:
    return f"digit_{d}"
