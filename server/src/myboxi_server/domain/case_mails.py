"""Mails around order requests for printed cases (docs/gehaeuse.md)."""

from __future__ import annotations

from urllib.parse import urlencode

from myboxi_case.config import CaseConfig
from myboxi_server.auth.mail import Mail, case_confirm_mail, case_notify_mail, case_receipt_mail
from myboxi_server.domain.case_requests import COUNTRIES, describe
from myboxi_server.models import CaseRequest
from myboxi_server.settings import Settings


def _base(settings: Settings) -> str:
    return settings.base_url.rstrip("/")


def confirm_mail(settings: Settings, req: CaseRequest, token: str) -> Mail:
    link = f"{_base(settings)}/gestalten/anfrage/bestaetigen?{urlencode({'token': token})}"
    return case_confirm_mail(req.email, req.contact_name, link)


def notify_mails(settings: Settings, req: CaseRequest) -> list[Mail]:
    """Operator mail plus receipt; empty when no operator address is configured."""
    if not settings.order_notify_email:
        return []
    cfg = CaseConfig.model_validate(req.config)
    query = urlencode(cfg.query())
    suffix = f"?{query}" if query else ""
    lines = [
        ("Name", req.contact_name),
        ("E-Mail", req.email),
        ("Land", COUNTRIES.get(req.country, req.country)),
        ("Anzahl", str(req.quantity)),
        *[(f"Gehäuse {label}", value) for label, value in describe(cfg)],
        ("Generator", req.generator_version),
        ("Nachricht", req.message or "(keine)"),
    ]
    return [
        case_notify_mail(
            settings.order_notify_email,
            req.email,
            lines,
            f"{_base(settings)}/gestalten{suffix}",
            f"{_base(settings)}/gestalten/download.zip{suffix}",
        ),
        case_receipt_mail(req.email, req.contact_name),
    ]
