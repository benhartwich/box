# pyright: reportUnknownMemberType=false
"""Pairing and device authentication (SPEC §7.1, §7.2)."""

from __future__ import annotations

import datetime as dt
import logging
import uuid

import httpx
import jwt
import pytest
from fastapi import FastAPI
from sqlalchemy import select, text, update

from box_protocol.errors import ErrorCode, ErrorResponse
from box_protocol.pairing import ClaimResponse, PairingClaimed, PairingPending
from box_server.logconfig import JsonFormatter
from box_server.models import Device, DeviceConfig, Pairing
from box_server.models.enums import Role

from .helpers import (
    add_member,
    claim_code,
    device_of_code,
    login,
    make_tenant,
    pair_device,
    sessionmaker_of,
    start_pairing,
)

POLL = "/api/v1/pairing/poll"


def error_code(r: httpx.Response) -> ErrorCode:
    return ErrorResponse.model_validate_json(r.content).error.code


async def _claim_api(
    client: httpx.AsyncClient, tid: uuid.UUID, csrf: str, code: str
) -> httpx.Response:
    return await client.post(
        f"/api/v1/tenants/{tid}/devices/claim",
        json={"code": code, "name": "Kinderzimmer"},
        headers={"X-CSRF-Token": csrf},
    )


async def test_full_pairing_via_claim_api(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    started = await start_pairing(client)
    assert len(started.code) == 6
    assert started.expires_in == 600
    r = await client.get(POLL, params={"poll_token": started.poll_token})
    assert r.status_code == 202
    assert PairingPending.model_validate_json(r.content).expires_in > 0

    csrf = await login(client, t.owner_email)
    r = await _claim_api(client, t.tenant_id, csrf, started.code)
    assert r.status_code == 200, r.text
    claim = ClaimResponse.model_validate_json(r.content)
    assert claim.name == "Kinderzimmer"

    await _drop_rate_limits(app)  # the box polls every 3 s; skip the wait
    r = await client.get(POLL, params={"poll_token": started.poll_token})
    assert r.status_code == 200
    assert "mqtt" not in r.json()  # M1: no broker
    claimed = PairingClaimed.model_validate_json(r.content)
    assert claimed.tenant_id == t.tenant_id

    # Exactly once.
    await _drop_rate_limits(app)
    r = await client.get(POLL, params={"poll_token": started.poll_token})
    assert r.status_code == 410
    assert error_code(r) == ErrorCode.PAIRING_CONSUMED

    async with sessionmaker_of(app)() as db:
        device = await db.get(Device, claim.device_id)
        assert device is not None
        assert device.tenant_id == t.tenant_id
        assert await db.get(DeviceConfig, claim.device_id) is not None


async def _drop_rate_limits(app: FastAPI) -> None:
    async with sessionmaker_of(app)() as db:
        await db.execute(text("DELETE FROM rate_limit"))
        await db.commit()


async def test_claim_api_requires_csrf(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    started = await start_pairing(client)
    await login(client, t.owner_email)
    r = await client.post(
        f"/api/v1/tenants/{t.tenant_id}/devices/claim", json={"code": started.code, "name": "x"}
    )
    assert r.status_code == 403
    ErrorResponse.model_validate_json(r.content)


async def test_claim_api_requires_login(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    r = await client.post(
        f"/api/v1/tenants/{t.tenant_id}/devices/claim", json={"code": "123456", "name": "x"}
    )
    assert r.status_code in (401, 403)
    ErrorResponse.model_validate_json(r.content)


async def test_expired_code_is_rejected(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    started = await start_pairing(client)
    async with sessionmaker_of(app)() as db:
        await db.execute(
            update(Pairing).values(expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1))
        )
        await db.commit()
    csrf = await login(client, t.owner_email)
    r = await _claim_api(client, t.tenant_id, csrf, started.code)
    assert r.status_code == 400
    assert error_code(r) == ErrorCode.CODE_INVALID
    r = await client.get(POLL, params={"poll_token": started.poll_token})
    assert r.status_code == 410
    assert error_code(r) == ErrorCode.PAIRING_EXPIRED


async def test_code_is_single_use(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    started = await start_pairing(client)
    csrf = await login(client, t.owner_email)
    assert (await _claim_api(client, t.tenant_id, csrf, started.code)).status_code == 200
    r = await _claim_api(client, t.tenant_id, csrf, started.code)
    assert r.status_code == 400
    assert error_code(r) == ErrorCode.CODE_INVALID


async def test_claim_brute_force_is_rate_limited(app: FastAPI, client: httpx.AsyncClient) -> None:
    """SPEC §7.1: 5 claims per minute and user; beyond that even the right code is refused."""
    t = await make_tenant(app)
    started = await start_pairing(client)
    csrf = await login(client, t.owner_email)
    wrong = f"{(int(started.code) + 1) % 1_000_000:06d}"
    statuses = [(await _claim_api(client, t.tenant_id, csrf, wrong)).status_code for _ in range(5)]
    assert statuses == [400] * 5
    r = await _claim_api(client, t.tenant_id, csrf, started.code)
    assert r.status_code == 429
    assert error_code(r) == ErrorCode.RATE_LIMITED
    assert int(r.headers["retry-after"]) > 0
    r = await client.get(POLL, params={"poll_token": started.poll_token})
    assert r.status_code == 202  # still unclaimed


async def test_claim_rate_limit_per_hour(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    async with sessionmaker_of(app)() as db:
        user_id = await db.scalar(text("SELECT user_id FROM membership LIMIT 1"))
        await db.execute(
            text(
                "INSERT INTO rate_limit (key, window_start, count) "
                "VALUES (:k, to_timestamp(floor(extract(epoch FROM now()) / 3600) * 3600), 20)"
            ),
            {"k": f"claim:user:{user_id}|3600"},
        )
        await db.commit()
    r = await _claim_api(client, t.tenant_id, csrf, "000000")
    assert r.status_code == 429


@pytest.mark.parametrize("role", [Role.CONTRIBUTOR, Role.VIEWER])
async def test_only_admins_claim(app: FastAPI, client: httpx.AsyncClient, role: Role) -> None:
    t = await make_tenant(app)
    started = await start_pairing(client)
    email = await add_member(app, t.tenant_id, role)
    csrf = await login(client, email)
    r = await _claim_api(client, t.tenant_id, csrf, started.code)
    assert r.status_code == 403


async def test_admin_claims(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    started = await start_pairing(client)
    csrf = await login(client, await add_member(app, t.tenant_id, Role.ADMIN))
    assert (await _claim_api(client, t.tenant_id, csrf, started.code)).status_code == 200


async def test_device_paired_elsewhere(app: FastAPI, client: httpx.AsyncClient) -> None:
    a = await make_tenant(app, "A")
    b = await make_tenant(app, "B")
    dev = await pair_device(app, client, a.tenant_id)
    started = await start_pairing(client, dev.device_id)
    csrf = await login(client, b.owner_email)
    r = await _claim_api(client, b.tenant_id, csrf, started.code)
    assert r.status_code == 409
    assert error_code(r) == ErrorCode.DEVICE_PAIRED_ELSEWHERE
    # The paired box keeps working.
    assert (await client.get("/api/v1/device/state", headers=dev.auth)).status_code == 200


async def test_repairing_same_tenant_rotates_credentials(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    t = await make_tenant(app)
    first = await pair_device(app, client, t.tenant_id)
    second = await pair_device(app, client, t.tenant_id, first.device_id)
    assert (await client.get("/api/v1/device/state", headers=first.auth)).status_code == 401
    r = await client.post(
        "/api/v1/device/token",
        json={"device_id": str(first.device_id), "device_secret": first.secret},
    )
    assert r.status_code == 401
    assert (await client.get("/api/v1/device/state", headers=second.auth)).status_code == 200


async def test_claim_invalidates_other_open_codes(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    device_id = uuid.uuid4()
    first = await start_pairing(client, device_id)
    second = await start_pairing(client, device_id)
    await claim_code(app, t.tenant_id, second.code)
    r = await client.get(POLL, params={"poll_token": first.poll_token})
    assert r.status_code == 410


async def test_poll_rate_limited(app: FastAPI, client: httpx.AsyncClient) -> None:
    started = await start_pairing(client)
    assert (await client.get(POLL, params={"poll_token": started.poll_token})).status_code == 202
    r = await client.get(POLL, params={"poll_token": started.poll_token})
    assert r.status_code == 429


async def test_unknown_poll_token(client: httpx.AsyncClient) -> None:
    r = await client.get(POLL, params={"poll_token": "x" * 43})
    assert r.status_code == 404
    assert error_code(r) == ErrorCode.NOT_FOUND


async def test_pairing_start_rate_limited_per_ip(client: httpx.AsyncClient) -> None:
    for _ in range(10):
        await start_pairing(client)
    r = await client.post(
        "/api/v1/pairing/start",
        json={"device_id": str(uuid.uuid4()), "hw_model": "rpi4", "agent_version": "0.1"},
    )
    assert r.status_code == 429


async def test_device_token_errors_are_uniform(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    wrong = await client.post(
        "/api/v1/device/token", json={"device_id": str(dev.device_id), "device_secret": "w" * 43}
    )
    unknown = await client.post(
        "/api/v1/device/token", json={"device_id": str(uuid.uuid4()), "device_secret": "w" * 43}
    )
    assert wrong.status_code == unknown.status_code == 401
    assert wrong.json() == unknown.json()
    assert error_code(wrong) == ErrorCode.INVALID_CREDENTIALS


async def test_device_token_rate_limited(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)  # one successful token request
    body = {"device_id": str(dev.device_id), "device_secret": "w" * 43}
    statuses = [
        (await client.post("/api/v1/device/token", json=body)).status_code for _ in range(10)
    ]
    assert statuses[:9] == [401] * 9
    assert statuses[9] == 429


async def test_jwt_validation(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    dev = await pair_device(app, client, t.tenant_id)
    claims = jwt.decode(dev.access_token, options={"verify_signature": False})
    assert claims["aud"] == "box-device"
    assert claims["exp"] - claims["iat"] == 3600
    key = app.state.settings.device_jwt_key.get_secret_value()
    forged = jwt.encode(claims | {"tid": str(uuid.uuid4())}, "another key of at least 32 bytes!!")
    expired = jwt.encode(claims | {"exp": claims["iat"] - 10}, key)
    other_tenant = jwt.encode(claims | {"tid": str(uuid.uuid4())}, key)
    for token in (forged, expired, other_tenant, "garbage"):
        r = await client.get("/api/v1/device/state", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 401
        assert error_code(r) == ErrorCode.UNAUTHORIZED
    assert (await client.get("/api/v1/device/state")).status_code == 401


async def test_secret_only_as_argon2id_and_never_logged(
    app: FastAPI, client: httpx.AsyncClient, caplog: pytest.LogCaptureFixture
) -> None:
    """SPEC §10: the device secret lives only as an Argon2id hash and never in a log."""
    caplog.set_level(logging.DEBUG)
    t = await make_tenant(app)
    started = await start_pairing(client)
    csrf = await login(client, t.owner_email)
    await _claim_api(client, t.tenant_id, csrf, started.code)
    r = await client.get(POLL, params={"poll_token": started.poll_token})
    secret = PairingClaimed.model_validate_json(r.content).device_secret
    device_id = await device_of_code(app, started.code)
    await client.post(
        "/api/v1/device/token", json={"device_id": str(device_id), "device_secret": secret}
    )

    async with sessionmaker_of(app)() as db:
        device = await db.get(Device, device_id)
        assert device is not None
        assert device.secret_hash is not None
        assert device.secret_hash.startswith("$argon2id$")
        # The plaintext appears in no column of any table.
        tables = (
            (
                await db.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = 'public'"
                    )
                )
            )
            .scalars()
            .all()
        )
        for table in tables:
            hit = await db.scalar(
                text(f'SELECT count(*) FROM "{table}" t WHERE t::text LIKE :s'),  # noqa: S608
                {"s": f"%{secret}%"},
            )
            assert hit == 0, table
        pairing = await db.scalar(select(Pairing).where(Pairing.code == started.code))
        assert pairing is not None
        assert started.poll_token.encode() not in pairing.poll_token_hash

    # Server-side records (the test's own httpx client logs its request URLs).
    server_records = [rec for rec in caplog.records if not rec.name.startswith("httpx")]
    assert any(rec.name == "box_server.access" for rec in server_records)
    raw = "\n".join(rec.getMessage() + repr(rec.__dict__) for rec in server_records)
    assert secret not in raw
    assert started.poll_token not in raw
    # And what would reach journald, including third-party records, is redacted.
    formatter = JsonFormatter()
    shipped = "\n".join(formatter.format(rec) for rec in caplog.records)
    assert secret not in shipped
    assert started.poll_token not in shipped
