"""Users, sessions, CSRF, invitations and roles (SPEC §3.2)."""

from __future__ import annotations

import datetime as dt
import io
import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from box_server.auth.tokens import hash_token
from box_server.cli import main as cli_main
from box_server.domain.authz import MIN_ROLE, Perm, role_can
from box_server.models import Invitation, Membership, User, WebSession
from box_server.models.enums import Role, RoleRank
from box_server.settings import Settings, get_settings

from .helpers import (
    PASSWORD,
    add_member,
    csrf_from,
    login,
    make_tenant,
    new_client,
    sessionmaker_of,
)

INVITE_RE = re.compile(r'value="(http://[^"]+/invite\?token=[^"]+)"')


async def test_login_sets_hardened_session_cookie(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    page = await client.get("/login")
    r = await client.post(
        "/login",
        data={"email": t.owner_email, "password": PASSWORD, "csrf_token": csrf_from(page.text)},
    )
    assert r.status_code == 303
    cookie = r.headers["set-cookie"]
    assert "box_session=" in cookie
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    # Only the hash is stored.
    value = client.cookies["box_session"]
    async with sessionmaker_of(app)() as db:
        assert await db.scalar(select(WebSession).where(WebSession.token_hash == hash_token(value)))


async def test_session_cookie_secure_flag(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    app.state.settings = app.state.settings.model_copy(update={"session_cookie_secure": True})
    page = await client.get("/login")
    r = await client.post(
        "/login",
        data={"email": t.owner_email, "password": PASSWORD, "csrf_token": csrf_from(page.text)},
    )
    assert "Secure" in r.headers["set-cookie"]


async def test_password_hash_is_argon2id(app: FastAPI) -> None:
    t = await make_tenant(app)
    async with sessionmaker_of(app)() as db:
        user = await db.scalar(select(User).where(User.email == t.owner_email))
        assert user is not None
        assert user.password_hash is not None
        assert user.password_hash.startswith("$argon2id$")


@pytest.mark.parametrize("email_kind", ["known", "unknown"])
async def test_login_rejects_bad_credentials(
    app: FastAPI, client: httpx.AsyncClient, email_kind: str
) -> None:
    t = await make_tenant(app)
    email = t.owner_email if email_kind == "known" else "nobody@example.org"
    page = await client.get("/login")
    r = await client.post(
        "/login",
        data={"email": email, "password": "wrong password", "csrf_token": csrf_from(page.text)},
    )
    assert r.status_code == 400
    assert "box_session" not in client.cookies


async def test_login_requires_csrf(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    await client.get("/login")
    r = await client.post("/login", data={"email": t.owner_email, "password": PASSWORD})
    assert r.status_code == 403


async def test_login_rate_limited_per_account(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    token = csrf_from((await client.get("/login")).text)
    statuses = [
        (
            await client.post(
                "/login",
                data={"email": t.owner_email, "password": "nope nope", "csrf_token": token},
            )
        ).status_code
        for _ in range(11)
    ]
    assert statuses[:10] == [400] * 10
    assert statuses[10] == 429
    # Even the right password is refused while limited.
    r = await client.post(
        "/login", data={"email": t.owner_email, "password": PASSWORD, "csrf_token": token}
    )
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) > 0


async def test_logout_invalidates_session(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    cookie = client.cookies["box_session"]
    r = await client.post("/logout", data={"csrf_token": csrf})
    assert r.status_code == 303
    client.cookies.set("box_session", cookie)
    r = await client.get(f"/t/{t.tenant_id}/")
    assert r.status_code == 303
    assert r.headers["location"].startswith("/login")


async def test_expired_session_is_rejected(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    await login(client, t.owner_email)
    async with sessionmaker_of(app)() as db:
        await db.execute(update(WebSession).values(expires_at=dt.datetime.now(dt.UTC)))
        await db.commit()
    r = await client.get(f"/t/{t.tenant_id}/")
    assert r.status_code == 303


async def test_authenticated_post_requires_csrf_and_origin(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    url = f"/t/{t.tenant_id}/members/invite"
    data = {"email": "x@example.org", "role": "viewer"}
    assert (await client.post(url, data=data)).status_code == 403
    assert (await client.post(url, data=data | {"csrf_token": "wrong"})).status_code == 403
    r = await client.post(
        url, data=data, headers={"X-CSRF-Token": csrf, "Origin": "https://evil.example"}
    )
    assert r.status_code == 403
    assert (await client.post(url, data=data, headers={"X-CSRF-Token": csrf})).status_code == 200


async def test_security_headers(client: httpx.AsyncClient) -> None:
    r = await client.get("/login")
    assert r.headers["x-frame-options"] == "DENY"
    assert "default-src 'self'" in r.headers["content-security-policy"]


async def _invite(client: httpx.AsyncClient, tid: object, csrf: str, email: str, role: str) -> str:
    r = await client.post(
        f"/t/{tid}/members/invite", data={"email": email, "role": role, "csrf_token": csrf}
    )
    assert r.status_code == 200, r.text
    m = INVITE_RE.search(r.text)
    assert m
    return m.group(1).split("token=", 1)[1]


async def test_invitation_creates_account_with_role(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    token = await _invite(client, t.tenant_id, csrf, "Oma@Example.org", "contributor")
    async with sessionmaker_of(app)() as db:
        inv = await db.scalar(select(Invitation))
        assert inv is not None
        assert inv.token_hash == hash_token(token)  # only the hash is stored

    async with new_client(app) as guest:
        page = await guest.get("/invite", params={"token": token})
        assert page.status_code == 200
        r = await guest.post(
            "/invite",
            data={
                "token": token,
                "display_name": "Oma",
                "password": "long enough password",
                "csrf_token": csrf_from(page.text),
            },
        )
        assert r.status_code == 303
        assert (await guest.get(f"/t/{t.tenant_id}/")).status_code == 200
        # Single use.
        again = await guest.get("/invite", params={"token": token})
        assert again.status_code == 404

    async with sessionmaker_of(app)() as db:
        user = await db.scalar(select(User).where(User.email == "oma@example.org"))
        assert user is not None
        m = await db.get(Membership, (t.tenant_id, user.id))
        assert m is not None
        assert m.role == Role.CONTRIBUTOR


async def test_expired_invitation_is_invalid(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    token = await _invite(client, t.tenant_id, csrf, "late@example.org", "viewer")
    async with sessionmaker_of(app)() as db:
        await db.execute(update(Invitation).values(expires_at=dt.datetime.now(dt.UTC)))
        await db.commit()
    async with new_client(app) as guest:
        assert (await guest.get("/invite", params={"token": token})).status_code == 404


async def test_invitation_for_existing_account_needs_matching_login(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    t = await make_tenant(app, "A")
    other = await make_tenant(app, "B")
    csrf = await login(client, t.owner_email)
    token = await _invite(client, t.tenant_id, csrf, other.owner_email, "admin")
    # The inviting owner (different email) cannot accept it.
    r = await client.post("/invite", data={"token": token, "csrf_token": csrf})
    assert r.status_code == 400
    async with new_client(app) as guest:
        # Anonymous: must sign in first, cannot create a second account.
        page = await guest.get("/invite", params={"token": token})
        assert "Bitte zuerst anmelden" in page.text
        guest_csrf = await login(guest, other.owner_email)
        r = await guest.post("/invite", data={"token": token, "csrf_token": guest_csrf})
        assert r.status_code == 303
        assert (await guest.get(f"/t/{t.tenant_id}/")).status_code == 200


def test_role_matrix_spec_3_2() -> None:
    assert MIN_ROLE.keys() == set(Perm)
    expect = {
        Role.VIEWER: {Perm.READ},
        Role.CONTRIBUTOR: {
            Perm.READ,
            Perm.TOKEN_WRITE,
            Perm.CONTENT_WRITE,
            Perm.BINDING_WRITE,
            Perm.UPLOAD,
        },
    }
    expect[Role.ADMIN] = expect[Role.CONTRIBUTOR] | {
        Perm.DEVICE_CLAIM,
        Perm.DEVICE_RENAME,
        Perm.DEVICE_CONFIG,
        Perm.DEVICE_REMOVE,
    }
    expect[Role.OWNER] = set(Perm)
    for role, perms in expect.items():
        assert {p for p in Perm if role_can(role, p)} == perms, role
    assert [r.rank for r in (Role.VIEWER, Role.CONTRIBUTOR, Role.ADMIN, Role.OWNER)] == sorted(
        RoleRank
    )


@pytest.mark.parametrize("role", [Role.VIEWER, Role.CONTRIBUTOR, Role.ADMIN])
async def test_only_owner_invites_and_manages(
    app: FastAPI, client: httpx.AsyncClient, role: Role
) -> None:
    t = await make_tenant(app)
    email = await add_member(app, t.tenant_id, role)
    csrf = await login(client, email)
    assert (await client.get(f"/t/{t.tenant_id}/members")).status_code == 200
    r = await client.post(
        f"/t/{t.tenant_id}/members/invite",
        data={"email": "x@example.org", "role": "viewer", "csrf_token": csrf},
    )
    assert r.status_code == 403
    async with sessionmaker_of(app)() as db:
        owner = await db.scalar(select(User).where(User.email == t.owner_email))
        assert owner is not None
    r = await client.post(f"/t/{t.tenant_id}/members/{owner.id}/remove", data={"csrf_token": csrf})
    assert r.status_code == 403


async def test_non_member_gets_404(app: FastAPI, client: httpx.AsyncClient) -> None:
    t = await make_tenant(app, "A")
    other = await make_tenant(app, "B")
    await login(client, other.owner_email)
    assert (await client.get(f"/t/{t.tenant_id}/members")).status_code == 404
    assert (await client.get(f"/t/{t.tenant_id}/")).status_code == 404


async def test_last_owner_cannot_leave_or_be_demoted(
    app: FastAPI, client: httpx.AsyncClient
) -> None:
    t = await make_tenant(app)
    csrf = await login(client, t.owner_email)
    async with sessionmaker_of(app)() as db:
        owner = await db.scalar(select(User).where(User.email == t.owner_email))
        assert owner is not None
    base = f"/t/{t.tenant_id}/members/{owner.id}"
    assert (
        await client.post(f"{base}/role", data={"role": "admin", "csrf_token": csrf})
    ).status_code == 400
    assert (await client.post(f"{base}/remove", data={"csrf_token": csrf})).status_code == 400


def test_create_admin_cli(app: FastAPI, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = app.state.settings
    monkeypatch.setenv("BOX_SERVER_DATABASE_URL", settings.database_url)
    monkeypatch.setenv("BOX_SERVER_DEVICE_JWT_KEY", settings.device_jwt_key.get_secret_value())
    monkeypatch.setattr("sys.stdin", io.StringIO("cli password 123\n"))
    get_settings.cache_clear()
    try:
        rc = cli_main(
            [
                "create-admin",
                "--email",
                "cli@example.org",
                "--tenant-name",
                "CLI",
                "--password-stdin",
            ]
        )
    finally:
        get_settings.cache_clear()
    assert rc == 0


async def test_smtp_invitation_is_sent_by_worker_with_rotated_token(
    app: FastAPI, client: httpx.AsyncClient, settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """SMTP mode: the page shows no link; the job rotates the token and mails the new link."""
    from box_server.auth import mail as mail_module
    from box_server.jobs import context as job_context

    sent: list[mail_module.Mail] = []

    def fake_send(_settings: Settings, mail: mail_module.Mail) -> None:
        sent.append(mail)

    monkeypatch.setattr("box_server.jobs.mail.send_smtp", fake_send)
    smtp_settings = settings.model_copy(update={"mail_backend": "smtp", "smtp_host": "localhost"})
    app.state.settings = smtp_settings
    job_context.configure(smtp_settings)
    try:
        t = await make_tenant(app)
        csrf = await login(client, t.owner_email)
        r = await client.post(
            f"/t/{t.tenant_id}/members/invite",
            data={"email": "opa@example.org", "role": "viewer", "csrf_token": csrf},
        )
        assert r.status_code == 200
        assert "/invite?token=" not in r.text
        await app.state.job_app.run_worker_async(
            queues=["mail"], wait=False, install_signal_handlers=False
        )
    finally:
        await job_context.dispose()
    assert len(sent) == 1
    assert sent[0].to == "opa@example.org"
    token = sent[0].body.split("token=", 1)[1].split()[0]
    async with new_client(app) as guest:
        assert (await guest.get("/invite", params={"token": token})).status_code == 200
