"""Roles and permissions (SPEC §3.2). The single place that maps permissions to roles."""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum

from myboxi_server.models.enums import Role, RoleRank


class Perm(StrEnum):
    READ = "read"
    TOKEN_WRITE = "token.write"
    CONTENT_WRITE = "content.write"
    BINDING_WRITE = "binding.write"
    UPLOAD = "upload"
    DEVICE_CLAIM = "device.claim"
    DEVICE_RENAME = "device.rename"
    DEVICE_CONFIG = "device.config"
    DEVICE_REMOVE = "device.remove"
    MEMBER_INVITE = "member.invite"
    MEMBER_MANAGE = "member.manage"


MIN_ROLE: dict[Perm, RoleRank] = {
    Perm.READ: RoleRank.VIEWER,
    # contributor: "Inhalte hochladen, Figuren anlegen und zuordnen"
    Perm.TOKEN_WRITE: RoleRank.CONTRIBUTOR,
    Perm.CONTENT_WRITE: RoleRank.CONTRIBUTOR,
    Perm.BINDING_WRITE: RoleRank.CONTRIBUTOR,
    Perm.UPLOAD: RoleRank.CONTRIBUTOR,
    # admin: "Boxen koppeln/entfernen, Regeln (Lautstärke, Ruhezeiten)"
    Perm.DEVICE_CLAIM: RoleRank.ADMIN,
    Perm.DEVICE_RENAME: RoleRank.ADMIN,
    Perm.DEVICE_CONFIG: RoleRank.ADMIN,
    Perm.DEVICE_REMOVE: RoleRank.ADMIN,
    # owner: "alles inkl. Abrechnung, Mandant löschen, Nutzer einladen"
    Perm.MEMBER_INVITE: RoleRank.OWNER,
    Perm.MEMBER_MANAGE: RoleRank.OWNER,
}


def role_can(role: Role, perm: Perm) -> bool:
    return role.rank >= MIN_ROLE[perm]


class PermissionDeniedError(Exception):
    pass


@dataclass(frozen=True)
class TenantContext:
    """Proof that ``user_id`` is a member of ``tenant_id`` with ``role``.

    Domain functions on tenant data require one and filter every query by ``tenant_id``.
    """

    tenant_id: uuid.UUID
    user_id: uuid.UUID | None
    role: Role

    def can(self, perm: Perm) -> bool:
        return role_can(self.role, perm)

    def require(self, perm: Perm) -> None:
        if not self.can(perm):
            raise PermissionDeniedError(perm)
