"""Order requests for printed cases: off by default, double opt-in, mails, retention."""

from __future__ import annotations

import datetime as dt
import re

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import select, update

from myboxi_server.auth import mail as mail_module
from myboxi_server.domain.retention import purge_expired
from myboxi_server.jobs import context as job_context
from myboxi_server.models import CaseRequest
from myboxi_server.models.enums import CaseRequestStatus
from myboxi_server.settings import Settings

from .helpers import csrf_from, sessionmaker_of

OPS = "werkstatt@example.org"
CONFIG = "form=bear&name=Mia"


@pytest.fixture
def orders_on(app: FastAPI, settings: Settings) -> Settings:
    on = settings.model_copy(update={"order_notify_email": OPS})
    app.state.settings = on
    return on


@pytest.fixture
def logged(monkeypatch: pytest.MonkeyPatch) -> list[mail_module.Mail]:
    """Mails of the log backend (development and tests)."""
    mails: list[mail_module.Mail] = []

    def capture(_settings: Settings, mail: mail_module.Mail) -> None:
        mails.append(mail)

    monkeypatch.setattr("myboxi_server.api.web.routes_case.log_mail", capture)
    return mails


async def _submit(client: httpx.AsyncClient, **extra: str) -> httpx.Response:
    page = await client.get(f"/gestalten/anfrage?{CONFIG}")
    assert page.status_code == 200
    data = {
        "csrf_token": csrf_from(page.text),
        "config": CONFIG,
        "name": "Anna Beispiel",
        "email": "Anna@Example.org",
        "country": "AT",
        "quantity": "2",
        "message": "Bitte in Braun.",
        "consent": "1",
    } | extra
    return await client.post("/gestalten/anfrage", data=data)


async def _requests(app: FastAPI) -> list[CaseRequest]:
    async with sessionmaker_of(app)() as db:
        return list((await db.scalars(select(CaseRequest))).all())


def _token(mail: mail_module.Mail) -> str:
    found = re.search(r"token=([\w-]+)", mail.body)
    assert found
    return found.group(1)


async def test_off_without_an_order_address(client: httpx.AsyncClient) -> None:
    assert (await client.get(f"/gestalten/anfrage?{CONFIG}")).status_code == 404
    assert "Gedruckt bestellen" not in (await client.get("/gestalten")).text


async def test_request_with_double_opt_in(
    app: FastAPI, client: httpx.AsyncClient, orders_on: Settings, logged: list[mail_module.Mail]
) -> None:
    page = await client.get(f"/gestalten?{CONFIG}")
    assert f'href="/gestalten/anfrage?{CONFIG.replace("&", "&amp;")}"' in page.text

    r = await _submit(client)
    assert r.status_code == 200
    assert "Fast geschafft" in r.text
    [req] = await _requests(app)
    assert req.status == CaseRequestStatus.UNCONFIRMED
    assert req.email == "anna@example.org"
    assert req.config["name"] == "Mia"
    [confirm] = logged
    assert confirm.to == "anna@example.org"
    token = _token(confirm)

    # Opening the link only shows a button (mail scanners must not confirm).
    shown = await client.get("/gestalten/anfrage/bestaetigen", params={"token": token})
    assert shown.status_code == 200
    assert (await _requests(app))[0].status == CaseRequestStatus.UNCONFIRMED

    done = await client.post(
        "/gestalten/anfrage/bestaetigen",
        data={"token": token, "csrf_token": csrf_from(shown.text)},
    )
    assert done.status_code == 200
    assert "Danke" in done.text
    [req] = await _requests(app)
    assert req.status == CaseRequestStatus.CONFIRMED
    assert req.token_hash is None
    notify, receipt = logged[1:]
    assert notify.to == OPS
    assert notify.reply_to == "anna@example.org"
    assert "Bär" in notify.body
    assert "Bitte in Braun." in notify.body
    assert "/gestalten/download.zip?form=bear&name=Mia" in notify.body
    assert receipt.to == "anna@example.org"

    again = await client.post(
        "/gestalten/anfrage/bestaetigen",
        data={"token": token, "csrf_token": csrf_from(shown.text)},
    )
    assert again.status_code == 404


async def test_refusals(
    app: FastAPI, client: httpx.AsyncClient, orders_on: Settings, logged: list[mail_module.Mail]
) -> None:
    r = await _submit(client, consent="")
    assert r.status_code == 400
    assert "bestätige" in r.text
    r = await _submit(client, country="US")
    assert r.status_code == 400
    r = await _submit(client, quantity="9")
    assert r.status_code == 400
    # Honeypot: looks like success, nothing stored, no mail.
    r = await _submit(client, website="http://spam.example")
    assert "Fast geschafft" in r.text
    assert await _requests(app) == []
    assert logged == []
    page = await client.get("/gestalten/anfrage?form=cube&power=powerbank")
    assert page.status_code == 303


async def test_smtp_mails_go_through_the_worker(
    app: FastAPI, client: httpx.AsyncClient, orders_on: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    sent: list[mail_module.Mail] = []

    def capture(_settings: Settings, mail: mail_module.Mail) -> None:
        sent.append(mail)

    monkeypatch.setattr("myboxi_server.jobs.mail.send_smtp", capture)
    smtp = orders_on.model_copy(update={"mail_backend": "smtp", "smtp_host": "localhost"})
    app.state.settings = smtp
    job_context.configure(smtp)
    try:
        assert (await _submit(client)).status_code == 200
        await app.state.job_app.run_worker_async(
            queues=["mail"], wait=False, install_signal_handlers=False
        )
        [confirm] = sent
        token = _token(confirm)
        shown = await client.get("/gestalten/anfrage/bestaetigen", params={"token": token})
        await client.post(
            "/gestalten/anfrage/bestaetigen",
            data={"token": token, "csrf_token": csrf_from(shown.text)},
        )
        await app.state.job_app.run_worker_async(
            queues=["mail"], wait=False, install_signal_handlers=False
        )
    finally:
        await job_context.dispose()
    assert [m.to for m in sent] == ["anna@example.org", OPS, "anna@example.org"]
    assert "Reply-To" in str(mail_module.build_message(smtp, sent[1]))


async def test_retention(
    app: FastAPI, client: httpx.AsyncClient, orders_on: Settings, logged: list[mail_module.Mail]
) -> None:
    await _submit(client)
    await _submit(client, email="bert@example.org")
    await _submit(client, email="cleo@example.org")
    reqs = await _requests(app)
    now = dt.datetime.now(dt.UTC)
    async with sessionmaker_of(app)() as db:
        # 1: never confirmed, link expired; 2: confirmed long ago; 3: confirmed recently.
        await db.execute(
            update(CaseRequest)
            .where(CaseRequest.id == reqs[0].id)
            .values(token_expires_at=now - dt.timedelta(hours=1))
        )
        await db.execute(
            update(CaseRequest)
            .where(CaseRequest.id.in_([reqs[1].id, reqs[2].id]))
            .values(status=CaseRequestStatus.CONFIRMED)
        )
        await db.commit()
        counts = await purge_expired(db, now)
        await db.commit()
    assert counts["case_request"] == 1  # the unconfirmed one
    async with sessionmaker_of(app)() as db:
        counts = await purge_expired(db, now + dt.timedelta(days=366))
        await db.commit()
    assert counts["case_request"] == 2  # a year after the last change
