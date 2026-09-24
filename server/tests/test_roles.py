"""Role rights per route (SPEC §3.2): every tenant route declares require(perm); each role is
refused exactly where the matrix says so. Picks up new routes automatically."""

from __future__ import annotations

import httpx
import pytest
from fastapi import FastAPI

from box_server.domain.authz import role_can
from box_server.models.enums import Role

from .helpers import add_member, login, make_tenant
from .routes import tenant_routes


@pytest.mark.parametrize("role", [Role.VIEWER, Role.CONTRIBUTOR, Role.ADMIN, Role.OWNER])
async def test_role_matrix_on_every_route(
    app: FastAPI, client: httpx.AsyncClient, role: Role
) -> None:
    t = await make_tenant(app)
    email = t.owner_email if role == Role.OWNER else await add_member(app, t.tenant_id, role)
    csrf = await login(client, email)
    for r in tenant_routes(app):
        assert r.perm is not None
        # Placeholder ids: allowed requests end in 404/400/422 but never in 403.
        url = r.url(
            {"tid": str(t.tenant_id)}
            | {p: "00000000-0000-7000-8000-000000000000" for p in r.params if p != "tid"}
        )
        resp = await client.request(r.method, url, headers={"X-CSRF-Token": csrf}, data={})
        allowed = role_can(role, r.perm)
        if allowed:
            assert resp.status_code != 403, f"{role} {r.method} {r.path}"
        else:
            assert resp.status_code == 403, f"{role} {r.method} {r.path} → {resp.status_code}"


async def test_viewer_cannot_change_anything(app: FastAPI) -> None:
    """Every state-changing tenant route needs more than viewer rights."""
    for r in tenant_routes(app):
        if r.method != "GET":
            assert r.perm is not None
            assert not role_can(Role.VIEWER, r.perm), f"{r.method} {r.path}"
