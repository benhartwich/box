"""Enumerations shared by models and domain code (SPEC §3)."""

from __future__ import annotations

from enum import IntEnum, StrEnum


class TenantPlan(StrEnum):
    SELF_HOSTED = "self_hosted"
    HOSTED_FREE = "hosted_free"
    HOSTED_PAID = "hosted_paid"


class Role(StrEnum):
    """SPEC §3.2. Ordering via ``Role.rank``."""

    OWNER = "owner"
    ADMIN = "admin"
    CONTRIBUTOR = "contributor"
    VIEWER = "viewer"

    @property
    def rank(self) -> RoleRank:
        return RoleRank[self.name]


class RoleRank(IntEnum):
    VIEWER = 10
    CONTRIBUTOR = 20
    ADMIN = 30
    OWNER = 40


class ContentKind(StrEnum):
    COLLECTION = "collection"
    PODCAST = "podcast"
    SPOTIFY = "spotify"
    STREAM = "stream"


class RepeatMode(StrEnum):
    OFF = "off"
    ALL = "all"
    ONE = "one"


class OnTokenRemoved(StrEnum):
    PAUSE = "pause"
    CONTINUE = "continue"


class UploadProfile(StrEnum):
    """SPEC §3.8: speech = mono 48 kbit/s, music = stereo 96 kbit/s."""

    SPEECH = "speech"
    MUSIC = "music"


class UploadStatus(StrEnum):
    PENDING = "pending"
    PROCESSING = "processing"
    DONE = "done"
    DUPLICATE = "duplicate"
    FAILED = "failed"


EVENT_TYPES = (
    "token_unknown",
    "token_played",
    "playback_error",
    "storage_full",
    "sync_error",
    "resume_position",
)
PROVIDERS = ("local", "podcast", "spotify", "stream")


class CaseRequestStatus(StrEnum):
    """Order requests for printed cases (docs/gehaeuse.md); outside the device protocol."""

    UNCONFIRMED = "unconfirmed"  # waiting for the e-mail confirmation
    CONFIRMED = "confirmed"  # the operator was notified
    ANSWERED = "answered"  # offer sent
    DONE = "done"
    CANCELLED = "cancelled"
