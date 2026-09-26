"""Device API client (SPEC §7). Every response is parsed with the protocol models."""

from __future__ import annotations

import asyncio
import hashlib
import ssl
import time
import uuid
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ValidationError

from myboxi_protocol.auth import DeviceTokenRequest, DeviceTokenResponse
from myboxi_protocol.errors import ErrorCode, ErrorResponse
from myboxi_protocol.events import EventBatchRequest, EventBatchResponse
from myboxi_protocol.pairing import (
    PairingClaimed,
    PairingPending,
    PairingStartRequest,
    PairingStartResponse,
)
from myboxi_protocol.reported import ReportedMessage
from myboxi_protocol.state import StateResponse

API = "/api/v1"
TOKEN_MARGIN_S = 120
CHUNK = 256 * 1024


class ApiError(Exception):
    def __init__(self, status: int, code: ErrorCode | None, message: str = "") -> None:
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code


class Unreachable(Exception):
    """Network or server not reachable: retry later, not a protocol error."""


class ChecksumMismatch(Exception):
    pass


def _error(response: httpx.Response) -> ApiError:
    try:
        body = ErrorResponse.model_validate_json(response.content)
        return ApiError(response.status_code, body.error.code, body.error.message)
    except ValidationError:
        return ApiError(response.status_code, None, response.text[:200])


class DeviceApi:
    def __init__(
        self,
        base_url: str,
        client: httpx.AsyncClient | None = None,
        verify: ssl.SSLContext | bool = True,
    ) -> None:
        """``verify``: with a self-hosted server's own CA, see ``sync/tls.py``."""
        self.base_url = base_url.rstrip("/")
        self.http = client or httpx.AsyncClient(
            timeout=httpx.Timeout(30, read=60), follow_redirects=False, verify=verify
        )
        self._token: str | None = None
        self._token_until = 0.0

    async def aclose(self) -> None:
        await self.http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            return await self.http.request(method, f"{self.base_url}{API}{path}", **kwargs)
        except httpx.TransportError as exc:
            raise Unreachable(str(exc)) from exc

    # --- pairing (§7.1) ------------------------------------------------------------------------

    async def pairing_start(self, body: PairingStartRequest) -> PairingStartResponse:
        r = await self._request("POST", "/pairing/start", json=body.model_dump(mode="json"))
        if r.status_code != 200:
            raise _error(r)
        return PairingStartResponse.model_validate_json(r.content)

    async def pairing_poll(self, poll_token: str) -> PairingPending | PairingClaimed:
        r = await self._request("GET", "/pairing/poll", params={"poll_token": poll_token})
        if r.status_code == 202:
            return PairingPending.model_validate_json(r.content)
        if r.status_code == 200:
            return PairingClaimed.model_validate_json(r.content)
        raise _error(r)

    # --- auth (§7.2) ---------------------------------------------------------------------------

    def forget_token(self) -> None:
        self._token, self._token_until = None, 0.0

    async def token(self, device_id: uuid.UUID, secret: str) -> str:
        if self._token and time.monotonic() < self._token_until:
            return self._token
        body = DeviceTokenRequest(device_id=device_id, device_secret=secret)
        r = await self._request("POST", "/device/token", json=body.model_dump(mode="json"))
        if r.status_code != 200:
            raise _error(r)
        token = DeviceTokenResponse.model_validate_json(r.content)
        self._token = token.access_token
        self._token_until = time.monotonic() + max(token.expires_in - TOKEN_MARGIN_S, 60)
        return self._token

    # --- device endpoints (§7.3) -----------------------------------------------------------------

    async def _authed(self, method: str, path: str, token: str, **kwargs: Any) -> httpx.Response:
        headers = kwargs.pop("headers", {}) | {"Authorization": f"Bearer {token}"}
        r = await self._request(method, path, headers=headers, **kwargs)
        if r.status_code == 401:
            self.forget_token()
        return r

    async def state(self, token: str, config_rev: int, device_rev: int) -> StateResponse:
        params = {"config_rev": config_rev, "device_rev": device_rev}
        r = await self._authed("GET", "/device/state", token, params=params)
        if r.status_code != 200:
            raise _error(r)
        return StateResponse.model_validate_json(r.content)

    async def events(self, token: str, envelopes: list[dict[str, Any]]) -> EventBatchResponse:
        batch = EventBatchRequest.model_validate({"events": envelopes})
        r = await self._authed("POST", "/device/events", token, json=batch.model_dump(mode="json"))
        if r.status_code != 200:
            raise _error(r)
        return EventBatchResponse.model_validate_json(r.content)

    async def reported(self, token: str, message: ReportedMessage) -> None:
        await self._post_no_content("/device/reported", token, message)

    async def unpair(self, token: str) -> None:
        await self._post_no_content("/device/unpair", token, None)

    async def _post_no_content(self, path: str, token: str, body: BaseModel | None) -> None:
        kwargs: dict[str, Any] = {"json": body.model_dump(mode="json")} if body else {}
        r = await self._authed("POST", path, token, **kwargs)
        if r.status_code != 204:
            raise _error(r)

    async def download(self, token: str, sha256: str, dest: Path) -> int:
        """Download into ``dest`` (SPEC §4.1): resume a partial file via Range, verify the
        SHA-256, then move into place atomically. Returns the size."""
        part = dest.with_name(dest.name + ".part")
        offset = await asyncio.to_thread(_prepare, part)
        headers = {"Authorization": f"Bearer {token}"}
        if offset:
            headers["Range"] = f"bytes={offset}-"
        url = f"{self.base_url}{API}/device/assets/{sha256}"
        try:
            async with self.http.stream("GET", url, headers=headers) as r:
                if r.status_code == 401:
                    self.forget_token()
                if r.status_code == 200:
                    offset = 0  # server ignored the range: start over
                elif r.status_code != 206:
                    await r.aread()
                    raise _error(r)
                with part.open("ab" if offset else "wb") as fh:
                    async for chunk in r.aiter_bytes(CHUNK):
                        fh.write(chunk)
        except httpx.TransportError as exc:
            raise Unreachable(str(exc)) from exc
        return await asyncio.to_thread(_finish, part, dest, sha256)


def _prepare(part: Path) -> int:
    part.parent.mkdir(parents=True, exist_ok=True)
    return part.stat().st_size if part.exists() else 0


def _finish(part: Path, dest: Path, sha256: str) -> int:
    if _sha256(part) != sha256:
        part.unlink(missing_ok=True)
        raise ChecksumMismatch(sha256)
    part.replace(dest)
    return dest.stat().st_size


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while chunk := fh.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()
