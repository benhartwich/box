"""Device API errors in the SPEC §7.4 format."""

from __future__ import annotations

from fastapi.responses import JSONResponse

from box_protocol.errors import ErrorBody, ErrorCode, ErrorResponse

_STATUS_CODES = {
    400: ErrorCode.INVALID_REQUEST,
    401: ErrorCode.UNAUTHORIZED,
    403: ErrorCode.UNAUTHORIZED,
    404: ErrorCode.NOT_FOUND,
    422: ErrorCode.INVALID_REQUEST,
    429: ErrorCode.RATE_LIMITED,
}


class ApiError(Exception):
    def __init__(
        self,
        status_code: int,
        code: ErrorCode,
        message: str,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers or {}


def error_response(
    status_code: int, code: ErrorCode, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    body = ErrorResponse(error=ErrorBody(code=code, message=message))
    return JSONResponse(body.model_dump(mode="json"), status_code=status_code, headers=headers)


def code_for_status(status_code: int) -> ErrorCode:
    return _STATUS_CODES.get(status_code, ErrorCode.INVALID_REQUEST)


def unauthorized(message: str = "Unauthorized") -> ApiError:
    return ApiError(401, ErrorCode.UNAUTHORIZED, message, {"WWW-Authenticate": "Bearer"})


def rate_limited(retry_after: int) -> ApiError:
    return ApiError(
        429, ErrorCode.RATE_LIMITED, "Too many requests", {"Retry-After": str(retry_after)}
    )
