"""Order requests for printed cases (docs/gehaeuse.md).

A request only becomes visible to the operator after the requester confirms their e-mail address
(double opt-in); mail scanners cannot confirm, because confirming needs a POST.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import delete, func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_case import GENERATOR_VERSION
from myboxi_case.config import PALETTE, ROLES, CaseConfig
from myboxi_server.auth.tokens import hash_token, new_token
from myboxi_server.domain.errors import InvalidInputError, NotFoundError
from myboxi_server.domain.members import normalize_email
from myboxi_server.models import CaseRequest
from myboxi_server.models.enums import CaseRequestStatus

CONFIRM_WINDOW = dt.timedelta(hours=48)
MAX_QUANTITY = 5
MAX_MESSAGE = 2000
MAX_NAME = 100
COUNTRIES = {"AT": "Österreich", "DE": "Deutschland", "CH": "Schweiz", "LI": "Liechtenstein"}

FORM_LABELS = {"radio": "Radio", "cube": "Würfel", "bear": "Bär"}
BOARD_LABELS = {"zero2w": "Raspberry Pi Zero 2 W", "pi4": "Raspberry Pi 4"}
POWER_LABELS = {"usbc": "USB-C-Buchse", "powerbank": "Powerbank im Gehäuse"}
GRILLE_LABELS = {"dots": "Punkte", "stars": "Sterne", "hearts": "Herzen", "lines": "Streifen"}


def describe(cfg: CaseConfig) -> list[tuple[str, str]]:
    """The configuration in words, for pages and mails."""
    return [
        ("Form", FORM_LABELS[cfg.form]),
        ("Name", cfg.name or "(ohne)"),
        (
            "Farben",
            ", ".join(PALETTE[cfg.color_key(role)][0] for role in ROLES),
        ),
        ("Gitter", GRILLE_LABELS[cfg.grille]),
        ("Platine", BOARD_LABELS[cfg.board]),
        ("Strom", POWER_LABELS[cfg.power]),
        ("Lautsprecher", f"{cfg.speaker} mm"),
        ("Taster", f"{cfg.button} mm"),
        ("Druck", "mehrfarbig" if cfg.colors == "multi" else "einfarbig"),
    ]


def _now() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


async def create_request(
    db: AsyncSession, cfg: CaseConfig, *, email: str, name: str, country: str,
    quantity: int, message: str,
) -> CaseRequest:  # fmt: skip
    """Stores an unconfirmed request; the caller sends the confirmation mail."""
    email = normalize_email(email)
    name = " ".join(name.split())
    if not 1 <= len(name) <= MAX_NAME:
        raise InvalidInputError("Bitte gib deinen Namen an.")
    if country not in COUNTRIES:
        raise InvalidInputError(
            "Wir liefern nach Österreich, Deutschland, in die Schweiz und nach Liechtenstein."
        )
    if not 1 <= quantity <= MAX_QUANTITY:
        raise InvalidInputError(f"Bitte eine Anzahl zwischen 1 und {MAX_QUANTITY} wählen.")
    message = message.strip()
    if len(message) > MAX_MESSAGE:
        raise InvalidInputError(f"Die Nachricht darf höchstens {MAX_MESSAGE} Zeichen haben.")
    req = CaseRequest(
        email=email,
        contact_name=name,
        country=country,
        quantity=quantity,
        message=message,
        config=cfg.model_dump(mode="json"),
        config_digest=cfg.digest(),
        generator_version=GENERATOR_VERSION,
        token_hash=hash_token(new_token()),  # replaced when the mail is sent
        token_expires_at=_now() + CONFIRM_WINDOW,
    )
    db.add(req)
    await db.flush()
    return req


async def rotate_token(db: AsyncSession, request_id: uuid.UUID) -> str | None:
    """A fresh confirmation token for an open request (the plaintext exists only in the mail)."""
    token = new_token()
    result = await db.execute(
        update(CaseRequest)
        .where(
            CaseRequest.id == request_id,
            CaseRequest.status == CaseRequestStatus.UNCONFIRMED,
            CaseRequest.token_expires_at > func.now(),
        )
        .values(token_hash=hash_token(token))
        .returning(CaseRequest.id)
    )
    return token if result.first() else None


async def open_request(db: AsyncSession, token: str) -> CaseRequest | None:
    if not token:
        return None
    return await db.scalar(
        select(CaseRequest).where(
            CaseRequest.token_hash == hash_token(token),
            CaseRequest.status == CaseRequestStatus.UNCONFIRMED,
            CaseRequest.token_expires_at > func.now(),
        )
    )


async def confirm(db: AsyncSession, token: str) -> CaseRequest | None:
    req = await open_request(db, token)
    if req is None:
        return None
    req.status = CaseRequestStatus.CONFIRMED
    req.confirmed_at = _now()
    req.token_hash = None
    await db.flush()
    return req


async def list_requests(
    db: AsyncSession, status: CaseRequestStatus | None = None
) -> list[CaseRequest]:
    stmt = select(CaseRequest).order_by(CaseRequest.created_at)
    if status is not None:
        stmt = stmt.where(CaseRequest.status == status)
    return list((await db.scalars(stmt)).all())


async def set_status(db: AsyncSession, request_id: uuid.UUID, status: CaseRequestStatus) -> None:
    req = await db.get(CaseRequest, request_id)
    if req is None:
        raise NotFoundError()
    req.status = status


async def delete_request(db: AsyncSession, request_id: uuid.UUID) -> None:
    result = await db.execute(
        delete(CaseRequest).where(CaseRequest.id == request_id).returning(CaseRequest.id)
    )
    if result.first() is None:
        raise NotFoundError()
