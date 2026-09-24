"""Tenants, users, memberships and invitations (SPEC §3.1, §3.2)."""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from box_server.auth.passwords import hash_secret_async, verify_secret_async
from box_server.auth.tokens import hash_token, new_token
from box_server.domain.authz import Perm, TenantContext
from box_server.domain.errors import ConflictError, InvalidInputError, NotFoundError
from box_server.models import Invitation, Membership, Tenant, User
from box_server.models.enums import Role

INVITATION_TTL = dt.timedelta(days=7)
MIN_PASSWORD_LENGTH = 10
MAX_PASSWORD_LENGTH = 1024


def normalize_email(email: str) -> str:
    email = email.strip().lower()
    if "@" not in email or len(email) > 254 or email.startswith("@") or email.endswith("@"):
        raise InvalidInputError("Bitte eine gültige E-Mail-Adresse angeben.")
    return email


def check_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise InvalidInputError(
            f"Das Passwort muss mindestens {MIN_PASSWORD_LENGTH} Zeichen haben."
        )


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def user_by_email(db: AsyncSession, email: str) -> User | None:
    return await db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))


async def create_tenant_with_owner(
    db: AsyncSession, *, tenant_name: str, email: str, display_name: str, password: str | None
) -> tuple[Tenant, User]:
    """CLI ``create-admin``: new tenant, owned by a new or existing user. The caller commits."""
    email = normalize_email(email)
    tenant = Tenant(name=tenant_name.strip() or email)
    db.add(tenant)
    user = await user_by_email(db, email)
    if user is None:
        if password is None:
            raise InvalidInputError("Für ein neues Konto ist ein Passwort nötig.")
        check_password(password)
        user = User(
            email=email,
            display_name=display_name.strip() or email,
            password_hash=await hash_secret_async(password),
        )
        db.add(user)
    await db.flush()
    db.add(Membership(tenant_id=tenant.id, user_id=user.id, role=Role.OWNER))
    await db.flush()
    return tenant, user


async def authenticate(db: AsyncSession, email: str, password: str) -> User | None:
    """Constant-time with respect to unknown accounts (a dummy hash is verified)."""
    user = await user_by_email(db, email)
    ok = await verify_secret_async(user.password_hash if user else None, password)
    if not ok or user is None or not user.is_active:
        return None
    return user


async def tenant_context(
    db: AsyncSession, tenant_id: uuid.UUID, user_id: uuid.UUID
) -> TenantContext | None:
    role = await db.scalar(
        select(Membership.role).where(
            Membership.tenant_id == tenant_id, Membership.user_id == user_id
        )
    )
    return None if role is None else TenantContext(tenant_id, user_id, role)


@dataclass(frozen=True)
class TenantMembership:
    tenant: Tenant
    role: Role


async def memberships_for_user(db: AsyncSession, user_id: uuid.UUID) -> list[TenantMembership]:
    rows = await db.execute(
        select(Tenant, Membership.role)
        .join(Membership, Membership.tenant_id == Tenant.id)
        .where(Membership.user_id == user_id)
        .order_by(Tenant.name)
    )
    return [TenantMembership(t, r) for t, r in rows.tuples()]


@dataclass(frozen=True)
class Member:
    user: User
    role: Role


async def list_members(db: AsyncSession, ctx: TenantContext) -> list[Member]:
    rows = await db.execute(
        select(User, Membership.role)
        .join(Membership, Membership.user_id == User.id)
        .where(Membership.tenant_id == ctx.tenant_id)
        .order_by(User.display_name)
    )
    return [Member(u, r) for u, r in rows.tuples()]


async def list_open_invitations(db: AsyncSession, ctx: TenantContext) -> list[Invitation]:
    ctx.require(Perm.MEMBER_INVITE)
    return list(
        await db.scalars(
            select(Invitation)
            .where(
                Invitation.tenant_id == ctx.tenant_id,
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
                Invitation.expires_at > func.now(),
            )
            .order_by(Invitation.created_at)
        )
    )


async def create_invitation(
    db: AsyncSession, ctx: TenantContext, email: str, role: Role
) -> tuple[Invitation, str]:
    """Returns the invitation and its one-time token (only its hash is stored)."""
    ctx.require(Perm.MEMBER_INVITE)
    email = normalize_email(email)
    existing = await user_by_email(db, email)
    if existing is not None and await tenant_context(db, ctx.tenant_id, existing.id):
        raise ConflictError("Diese Person ist bereits Mitglied.")
    await db.execute(
        update(Invitation)
        .where(
            Invitation.tenant_id == ctx.tenant_id,
            func.lower(Invitation.email) == email,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
    )
    token = new_token()
    inv = Invitation(
        tenant_id=ctx.tenant_id,
        email=email,
        role=role,
        token_hash=hash_token(token),
        invited_by=ctx.user_id,
        expires_at=_now() + INVITATION_TTL,
    )
    db.add(inv)
    await db.flush()
    return inv, token


async def rotate_invitation_token(db: AsyncSession, invitation_id: uuid.UUID) -> str | None:
    """New token for an open invitation (used by the mail job, so no plaintext is queued)."""
    token = new_token()
    result = await db.execute(
        update(Invitation)
        .where(
            Invitation.id == invitation_id,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
            Invitation.expires_at > func.now(),
        )
        .values(token_hash=hash_token(token))
        .returning(Invitation.id)
    )
    return token if result.first() else None


async def revoke_invitation(db: AsyncSession, ctx: TenantContext, invitation_id: uuid.UUID) -> None:
    ctx.require(Perm.MEMBER_INVITE)
    result = await db.execute(
        update(Invitation)
        .where(
            Invitation.id == invitation_id,
            Invitation.tenant_id == ctx.tenant_id,
            Invitation.accepted_at.is_(None),
            Invitation.revoked_at.is_(None),
        )
        .values(revoked_at=func.now())
        .returning(Invitation.id)
    )
    if result.first() is None:
        raise NotFoundError()


@dataclass(frozen=True)
class OpenInvitation:
    invitation: Invitation
    tenant: Tenant
    existing_user: User | None


async def open_invitation(db: AsyncSession, token: str) -> OpenInvitation | None:
    row = (
        await db.execute(
            select(Invitation, Tenant)
            .join(Tenant, Tenant.id == Invitation.tenant_id)
            .where(
                Invitation.token_hash == hash_token(token),
                Invitation.accepted_at.is_(None),
                Invitation.revoked_at.is_(None),
                Invitation.expires_at > func.now(),
            )
        )
    ).one_or_none()
    if row is None:
        return None
    inv, tenant = row
    return OpenInvitation(inv, tenant, await user_by_email(db, inv.email))


async def accept_invitation(
    db: AsyncSession,
    token: str,
    *,
    current_user: User | None,
    display_name: str = "",
    password: str | None = None,
) -> tuple[User, uuid.UUID]:
    """Accept as the signed-in user (email must match) or by creating a new account.

    Existing accounts must sign in first. The caller commits.
    """
    found = await open_invitation(db, token)
    if found is None:
        raise NotFoundError("Diese Einladung ist ungültig oder abgelaufen.")
    inv = found.invitation
    if current_user is not None:
        if current_user.email.lower() != inv.email.lower():
            raise ConflictError("Diese Einladung gilt für eine andere E-Mail-Adresse.")
        user = current_user
    elif found.existing_user is not None:
        raise ConflictError("Für diese E-Mail-Adresse gibt es schon ein Konto. Bitte anmelden.")
    else:
        if password is None:
            raise InvalidInputError("Bitte ein Passwort festlegen.")
        check_password(password)
        user = User(
            email=inv.email,
            display_name=display_name.strip() or inv.email,
            password_hash=await hash_secret_async(password),
        )
        db.add(user)
        await db.flush()
    membership = await db.get(Membership, (inv.tenant_id, user.id))
    if membership is None:
        db.add(Membership(tenant_id=inv.tenant_id, user_id=user.id, role=inv.role))
    elif inv.role.rank > membership.role.rank:
        membership.role = inv.role
    inv.accepted_at = _now()
    await db.flush()
    return user, inv.tenant_id


async def _owner_count(db: AsyncSession, tenant_id: uuid.UUID) -> int:
    return (
        await db.scalar(
            select(func.count())
            .select_from(Membership)
            .where(Membership.tenant_id == tenant_id, Membership.role == Role.OWNER)
        )
    ) or 0


async def _membership(db: AsyncSession, ctx: TenantContext, user_id: uuid.UUID) -> Membership:
    # Serialize owner changes per tenant so two owners cannot demote each other concurrently.
    await db.execute(select(Tenant.id).where(Tenant.id == ctx.tenant_id).with_for_update())
    m = await db.scalar(
        select(Membership)
        .where(Membership.tenant_id == ctx.tenant_id, Membership.user_id == user_id)
        .with_for_update()
    )
    if m is None:
        raise NotFoundError()
    return m


async def change_role(db: AsyncSession, ctx: TenantContext, user_id: uuid.UUID, role: Role) -> None:
    ctx.require(Perm.MEMBER_MANAGE)
    m = await _membership(db, ctx, user_id)
    if m.role == Role.OWNER and role != Role.OWNER and await _owner_count(db, ctx.tenant_id) <= 1:
        raise ConflictError("Es muss mindestens eine Person mit der Rolle Owner geben.")
    m.role = role
    await db.flush()


async def remove_member(db: AsyncSession, ctx: TenantContext, user_id: uuid.UUID) -> None:
    ctx.require(Perm.MEMBER_MANAGE)
    m = await _membership(db, ctx, user_id)
    if m.role == Role.OWNER and await _owner_count(db, ctx.tenant_id) <= 1:
        raise ConflictError("Es muss mindestens eine Person mit der Rolle Owner geben.")
    await db.delete(m)
    await db.flush()
