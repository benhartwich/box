"""Error responses of the device API (SPEC §7.4)."""

from __future__ import annotations

from enum import StrEnum

from myboxi_protocol.common import ProtocolModel


class ErrorCode(StrEnum):
    INVALID_REQUEST = "invalid_request"
    UNAUTHORIZED = "unauthorized"
    INVALID_CREDENTIALS = "invalid_credentials"
    NOT_FOUND = "not_found"
    RATE_LIMITED = "rate_limited"
    PAIRING_EXPIRED = "pairing_expired"
    PAIRING_CONSUMED = "pairing_consumed"
    CODE_INVALID = "code_invalid"
    DEVICE_PAIRED_ELSEWHERE = "device_paired_elsewhere"
    PAIRING_DENIED = "pairing_denied"  # SPEC v0.12 §7.1: wrong or missing pairing key


class ErrorBody(ProtocolModel):
    code: ErrorCode
    message: str


class ErrorResponse(ProtocolModel):
    error: ErrorBody
