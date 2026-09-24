"""Shared field types (SPEC §3, §6.0)."""

from __future__ import annotations

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

PROTOCOL_VERSION: Literal[1] = 1

# SPEC §3.5: NFC UID, hex, upper case, no separators (4, 7 or 10 byte UIDs; any 4..10 bytes).
NfcUid = Annotated[str, StringConstraints(pattern=r"^(?:[0-9A-F]{2}){4,10}$")]
# SPEC §3.8: lower-case hex SHA-256.
Sha256Hex = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
# SPEC §6.0: ULID, 26 characters Crockford Base32.
Ulid = Annotated[str, StringConstraints(pattern=r"^[0-7][0-9A-HJKMNP-TV-Z]{25}$")]
# SPEC §3.4: "19:30"
ClockTime = Annotated[str, StringConstraints(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d$")]
HttpUrlStr = Annotated[str, StringConstraints(pattern=r"^https?://\S+$", max_length=2048)]
ShortText = Annotated[str, StringConstraints(min_length=1, max_length=200)]
Percent = Annotated[int, Field(ge=0, le=100)]
NonNegativeInt = Annotated[int, Field(ge=0)]

ProviderName = Literal["local", "podcast", "spotify", "stream"]


class ProtocolModel(BaseModel):
    """Base for all protocol messages.

    Unknown fields are ignored so that additive changes (SPEC §11) do not break older peers.
    """

    model_config = ConfigDict(extra="ignore", frozen=True)
