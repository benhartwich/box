"""Pairing and device authentication (SPEC §7.1, §7.2).

* ``start``: a box asks for a 6-digit code (10 min, single use) and a poll token.
* ``claim``: an admin of a tenant claims the code. No secret exists yet.
* ``poll``: the first poll after the claim creates the device secret, stores only its
  Argon2id hash and returns the plaintext exactly once.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import hmac
import secrets
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_server.auth.passwords import hash_secret_async, verify_secret_async
from myboxi_server.auth.tokens import hash_token, new_token
from myboxi_server.domain.authz import Perm, TenantContext
from myboxi_server.domain.errors import ConflictError, DomainError, NotFoundError
from myboxi_server.models import Device, DeviceConfig, Pairing

CODE_TTL = dt.timedelta(minutes=10)
# A claimed code stays deliverable for this long after the claim.
DELIVERY_TTL = dt.timedelta(minutes=10)
_CODE_ATTEMPTS = 20


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


@dataclass(frozen=True)
class StartedPairing:
    code: str
    poll_token: str
    expires_in: int


class PairingDeniedError(DomainError):
    """SPEC v0.12 §7.1: the device id is known with another pairing key."""


def _key_hash(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()  # 256 random bits: no slow hash needed


async def start(
    db: AsyncSession,
    *,
    device_id: uuid.UUID,
    hw_model: str,
    agent_version: str,
    pairing_key: str | None = None,
) -> StartedPairing:
    """SPEC §7.1 step 1. The caller commits.

    The first start with a pairing key binds the device id to it (trust on first use); later
    starts must present the same key, so knowing a device id is not enough to take a box."""
    device = await db.get(Device, device_id, with_for_update=True)
    if device is None:
        device = Device(id=device_id, hw_model=hw_model, agent_version=agent_version)
        db.add(device)
    if device.pairing_key_hash is not None:
        if pairing_key is None or not hmac.compare_digest(
            device.pairing_key_hash, _key_hash(pairing_key)
        ):
            raise PairingDeniedError("pairing key does not match")
    elif pairing_key is not None:
        device.pairing_key_hash = _key_hash(pairing_key)
    await db.flush()
    # Retire expired open codes so their numbers can be reused.
    await db.execute(
        update(Pairing)
        .where(
            Pairing.claimed_at.is_(None),
            Pairing.invalidated_at.is_(None),
            Pairing.expires_at <= func.now(),
        )
        .values(invalidated_at=func.now())
    )
    poll_token = new_token()
    for _ in range(_CODE_ATTEMPTS):
        code = f"{secrets.randbelow(1_000_000):06d}"
        try:
            async with db.begin_nested():
                db.add(
                    Pairing(
                        device_id=device_id,
                        code=code,
                        poll_token_hash=hash_token(poll_token),
                        hw_model=hw_model,
                        agent_version=agent_version,
                        expires_at=_now() + CODE_TTL,
                    )
                )
        except IntegrityError:
            continue  # code collision among open codes
        return StartedPairing(code, poll_token, int(CODE_TTL.total_seconds()))
    raise ConflictError("no free pairing code")


class CodeInvalidError(DomainError):
    pass


class PairedElsewhereError(DomainError):
    pass


async def claim(db: AsyncSession, ctx: TenantContext, *, code: str, name: str) -> Device:
    """SPEC §7.1 step 2 (role >= admin). Expired, used and unknown codes look the same."""
    ctx.require(Perm.DEVICE_CLAIM)
    pairing = await db.scalar(
        select(Pairing)
        .where(
            Pairing.code == code,
            Pairing.claimed_at.is_(None),
            Pairing.invalidated_at.is_(None),
            Pairing.expires_at > func.now(),
        )
        .with_for_update()
    )
    if pairing is None:
        raise CodeInvalidError("Der Code ist ungültig oder abgelaufen.")
    device = await db.get(Device, pairing.device_id, with_for_update=True)
    if device is None:  # pragma: no cover - FK guarantees existence
        raise CodeInvalidError("Der Code ist ungültig oder abgelaufen.")
    if device.tenant_id is not None and device.tenant_id != ctx.tenant_id:
        raise PairedElsewhereError("Diese Box ist mit einem anderen Haushalt gekoppelt.")

    now = _now()
    pairing.claimed_at = now
    pairing.claimed_by = ctx.user_id
    pairing.tenant_id = ctx.tenant_id
    pairing.device_name = name
    await db.execute(
        update(Pairing)
        .where(
            Pairing.device_id == device.id,
            Pairing.id != pairing.id,
            Pairing.claimed_at.is_(None),
            Pairing.invalidated_at.is_(None),
        )
        .values(invalidated_at=func.now())
    )
    device.tenant_id = ctx.tenant_id
    device.name = name
    device.hw_model = pairing.hw_model
    device.agent_version = pairing.agent_version
    device.secret_hash = None  # a previous secret stops working immediately
    device.auth_generation += 1
    device.paired_at = now
    await db.flush()
    if await db.get(DeviceConfig, device.id) is None:
        db.add(DeviceConfig(device_id=device.id, tenant_id=ctx.tenant_id))  # SPEC §3.4 defaults
        await db.flush()
    return device


@dataclass(frozen=True)
class PollPending:
    expires_in: int


@dataclass(frozen=True)
class PollClaimed:
    device_secret: str
    tenant_id: uuid.UUID
    device_id: uuid.UUID


class PairingExpiredError(DomainError):
    pass


class PairingConsumedError(DomainError):
    pass


async def poll(db: AsyncSession, poll_token: str) -> PollPending | PollClaimed:
    """SPEC §7.1 step 3. The caller commits."""
    pairing = await db.scalar(
        select(Pairing).where(Pairing.poll_token_hash == hash_token(poll_token)).with_for_update()
    )
    if pairing is None:
        raise NotFoundError("unknown poll token")
    now = _now()
    if pairing.delivered_at is not None:
        raise PairingConsumedError("secret already delivered")
    if pairing.invalidated_at is not None:
        raise PairingExpiredError("pairing expired")
    if pairing.claimed_at is None:
        if pairing.expires_at <= now:
            raise PairingExpiredError("pairing expired")
        return PollPending(int((pairing.expires_at - now).total_seconds()))
    if pairing.claimed_at + DELIVERY_TTL <= now:
        raise PairingExpiredError("pairing expired")
    device = await db.get(Device, pairing.device_id, with_for_update=True)
    if device is None or device.tenant_id is None or device.tenant_id != pairing.tenant_id:
        raise PairingExpiredError("pairing expired")  # unpaired again in the meantime
    secret = secrets.token_urlsafe(32)
    device.secret_hash = await hash_secret_async(secret)
    pairing.delivered_at = now
    await db.flush()
    return PollClaimed(device_secret=secret, tenant_id=device.tenant_id, device_id=device.id)


async def authenticate_device(db: AsyncSession, device_id: uuid.UUID, secret: str) -> Device | None:
    """SPEC §7.2. Unknown, unpaired and wrong secret are indistinguishable."""
    device = await db.get(Device, device_id)
    ok = await verify_secret_async(device.secret_hash if device else None, secret)
    if not ok or device is None or device.tenant_id is None:
        return None
    return device
