"""Device authentication (SPEC §7.2)."""

from __future__ import annotations

from typing import Annotated, Literal
from uuid import UUID

from pydantic import Field, StringConstraints

from myboxi_protocol.common import NonNegativeInt, ProtocolModel


class DeviceTokenRequest(ProtocolModel):
    device_id: UUID
    device_secret: Annotated[str, StringConstraints(min_length=16, max_length=256)] = Field(
        repr=False
    )


class DeviceTokenResponse(ProtocolModel):
    access_token: str = Field(repr=False)
    token_type: Literal["Bearer"] = "Bearer"  # noqa: S105
    expires_in: NonNegativeInt
