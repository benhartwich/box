"""Device HTTPS API (SPEC §7). Responses are built from ``myboxi_protocol`` models."""

from __future__ import annotations

import datetime as dt
import secrets
import uuid
from typing import Annotated

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response
from sqlalchemy import select

from myboxi_protocol.auth import DeviceTokenRequest, DeviceTokenResponse
from myboxi_protocol.errors import ErrorCode
from myboxi_protocol.events import EventBatchRequest, EventBatchResponse
from myboxi_protocol.pairing import (
    MqttCredentials,
    PairingClaimed,
    PairingPending,
    PairingStartRequest,
    PairingStartResponse,
)
from myboxi_protocol.reported import ReportedMessage
from myboxi_protocol.state import StateResponse
from myboxi_server.api.device.deps import CurrentDevice
from myboxi_server.api.device.errors import ApiError, rate_limited
from myboxi_server.api.device.jwt import DeviceClaims, issue
from myboxi_server.api.web.deps import DbSession, SettingsDep, client_ip
from myboxi_server.auth import ratelimit
from myboxi_server.auth.secretbox import seal
from myboxi_server.auth.tokens import hash_token
from myboxi_server.domain import devices, events, pairing, state
from myboxi_server.domain.errors import NotFoundError
from myboxi_server.models import Asset, Device
from myboxi_server.mqtt.service import PURPOSE as MQTT_PURPOSE
from myboxi_server.settings import Settings
from myboxi_server.storage.base import SHA256_RE
from myboxi_server.storage.filesystem import FilesystemAssetStore

router = APIRouter(prefix="/api/v1")


def _not_found() -> ApiError:
    return ApiError(404, ErrorCode.NOT_FOUND, "Not found")


async def _limit(request: Request, key: str, limits: tuple[ratelimit.Limit, ...]) -> None:
    try:
        await ratelimit.hit(request.app.state.engine, key, limits)
    except ratelimit.RateLimitedError as exc:
        raise rate_limited(exc.retry_after) from None


@router.post("/pairing/start", response_model=PairingStartResponse)
async def pairing_start(
    request: Request, body: PairingStartRequest, db: DbSession, settings: SettingsDep
) -> PairingStartResponse:
    """SPEC §7.1 step 1."""
    await _limit(
        request, f"pairing_start:ip:{client_ip(request, settings)}", ratelimit.PAIRING_START_PER_IP
    )
    started = await pairing.start(
        db, device_id=body.device_id, hw_model=body.hw_model, agent_version=body.agent_version
    )
    await db.commit()
    return PairingStartResponse(
        code=started.code, expires_in=started.expires_in, poll_token=started.poll_token
    )


@router.get(
    "/pairing/poll",
    response_model=PairingClaimed,
    responses={202: {"model": PairingPending}},
)
async def pairing_poll(
    request: Request,
    db: DbSession,
    settings: SettingsDep,
    poll_token: Annotated[str, Query(max_length=128)],
) -> Response | PairingClaimed:
    """SPEC §7.1 step 3: 202 pending, 200 with the secret exactly once, 410 afterwards."""
    await _limit(request, f"poll:{hash_token(poll_token).hex()}", ratelimit.POLL_PER_TOKEN)
    try:
        result = await pairing.poll(db, poll_token)
    except NotFoundError:
        raise _not_found() from None
    except pairing.PairingConsumedError:
        raise ApiError(410, ErrorCode.PAIRING_CONSUMED, "Secret already delivered") from None
    except pairing.PairingExpiredError:
        raise ApiError(410, ErrorCode.PAIRING_EXPIRED, "Pairing code expired") from None
    if isinstance(result, pairing.PollPending):
        await db.rollback()
        body = PairingPending(expires_in=result.expires_in)
        return JSONResponse(body.model_dump(mode="json"), status_code=202)
    mqtt = await _mqtt_account(db, settings, result.device_id)
    await db.commit()
    return PairingClaimed(device_secret=result.device_secret, tenant_id=result.tenant_id, mqtt=mqtt)


async def _mqtt_account(
    db: DbSession, settings: Settings, device_id: uuid.UUID
) -> MqttCredentials | None:
    """SPEC §7.1: credentials exactly once, with the device secret. The MQTT service creates
    the broker account within a second; until then the box's connect attempts fail and retry."""
    if not settings.mqtt_enabled:
        return None
    device = await db.get(Device, device_id)
    if device is None:  # pragma: no cover - the poll just delivered its secret
        return None
    password = secrets.token_urlsafe(32)
    device.mqtt_password = seal(settings, MQTT_PURPOSE, password)
    device.mqtt_revoke = False
    host = settings.mqtt_public_host or settings.mqtt_host or ""
    return MqttCredentials(
        host=host, port=settings.mqtt_port, username=str(device_id), password=password
    )


@router.post("/device/token", response_model=DeviceTokenResponse)
async def device_token(
    request: Request, body: DeviceTokenRequest, db: DbSession, settings: SettingsDep
) -> DeviceTokenResponse:
    """SPEC §7.2."""
    await _limit(request, f"device_token:{body.device_id}", ratelimit.DEVICE_TOKEN_PER_DEVICE)
    device = await pairing.authenticate_device(db, body.device_id, body.device_secret)
    if device is None or device.tenant_id is None:
        raise ApiError(401, ErrorCode.INVALID_CREDENTIALS, "Invalid credentials")
    device.last_seen_at = dt.datetime.now(dt.UTC)
    claims = DeviceClaims(device.id, device.tenant_id, device.auth_generation)
    await db.commit()
    ttl = settings.device_jwt_ttl_s
    return DeviceTokenResponse(
        access_token=issue(settings.device_jwt_key.get_secret_value(), claims, ttl),
        expires_in=ttl,
    )


@router.get("/device/state", response_model=StateResponse)
async def device_state(
    request: Request,
    device: CurrentDevice,
    config_rev: Annotated[int | None, Query(ge=0)] = None,
    device_rev: Annotated[int | None, Query(ge=0)] = None,
) -> StateResponse:
    """SPEC §5.4. M1 always returns a full snapshot; the box's revisions are accepted but unused."""
    del config_rev, device_rev
    return await state.snapshot(request.app.state.engine, device.tenant_id, device.device_id)


@router.get("/device/assets/{sha256}", response_class=Response)
async def device_asset(
    request: Request, sha256: str, device: CurrentDevice, db: DbSession
) -> Response:
    """SPEC §7.3: only assets of the box's own tenant; ETag = sha256, Range via nginx/Starlette."""
    if not SHA256_RE.match(sha256):
        raise _not_found()
    row = (
        await db.execute(
            select(Asset.storage_path, Asset.mime).where(
                Asset.tenant_id == device.tenant_id, Asset.sha256 == sha256
            )
        )
    ).one_or_none()
    if row is None:
        raise _not_found()
    store: FilesystemAssetStore = request.app.state.asset_store
    return store.response(request, row.storage_path, etag=sha256, media_type=row.mime)


@router.post("/device/events", response_model=EventBatchResponse)
async def device_events(
    body: EventBatchRequest, device: CurrentDevice, db: DbSession
) -> EventBatchResponse:
    """SPEC §7.3: batch of up to 100 events, deduplicated by id."""
    results = await events.ingest(
        db, tenant_id=device.tenant_id, device_id=device.device_id, events=body.events
    )
    await db.commit()
    return EventBatchResponse(results=results)


@router.post("/device/reported", status_code=204, response_class=Response)
async def device_reported(body: ReportedMessage, device: CurrentDevice, db: DbSession) -> Response:
    """SPEC §6.4/§7.3 (v0.3): HTTP fallback for ``reported``."""
    row = await db.get(Device, device.device_id, with_for_update=True)
    if row is None or row.tenant_id != device.tenant_id:  # pragma: no cover - checked in deps
        raise _not_found()
    devices.store_reported(row, body.data, dt.datetime.now(dt.UTC))
    await db.commit()
    return Response(status_code=204)


@router.post("/device/unpair", status_code=204, response_class=Response)
async def device_unpair(device: CurrentDevice, db: DbSession) -> Response:
    """SPEC §7.3: the box leaves its tenant; its credentials stop working."""
    row = await db.get(Device, device.device_id, with_for_update=True)
    if row is None or row.tenant_id != device.tenant_id:  # pragma: no cover - checked in deps
        raise _not_found()
    await devices.unpair(db, row)
    await db.commit()
    return Response(status_code=204)
