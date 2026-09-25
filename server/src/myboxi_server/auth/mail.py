"""Outgoing mail. ``log`` backend (development) writes the mail to the log; ``smtp`` sends it
from a background job (jobs/mail.py) so requests never wait on the mail server."""

from __future__ import annotations

import logging
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

from myboxi_server.settings import Settings

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Mail:
    to: str
    subject: str
    body: str
    reply_to: str | None = None


def build_message(settings: Settings, mail: Mail) -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = settings.mail_from
    msg["To"] = mail.to
    msg["Subject"] = mail.subject
    if mail.reply_to:
        msg["Reply-To"] = mail.reply_to
    msg.set_content(mail.body)
    return msg


def send_smtp(settings: Settings, mail: Mail) -> None:
    """Blocking SMTP delivery; call from a worker thread or job."""
    if not settings.smtp_host:
        raise RuntimeError("smtp_host not configured")
    msg = build_message(settings, mail)
    with smtplib.SMTP(settings.smtp_host, settings.smtp_port, timeout=30) as smtp:
        if settings.smtp_starttls:
            smtp.starttls(context=ssl.create_default_context())
        if settings.smtp_user and settings.smtp_password:
            smtp.login(settings.smtp_user, settings.smtp_password.get_secret_value())
        smtp.send_message(msg)


def log_mail(settings: Settings, mail: Mail) -> None:
    """``log`` backend. The body may contain one-time links, so it is logged in dev mode only."""
    if settings.is_dev:
        log.info("mail (log backend) to=%s subject=%s\n%s", mail.to, mail.subject, mail.body)
    else:
        log.info("mail not sent (log backend) to=%s subject=%s", mail.to, mail.subject)


def invitation_mail(to: str, tenant_name: str, link: str) -> Mail:
    return Mail(
        to=to,
        subject=f"Einladung zu {tenant_name} (Myboxi)",
        body=(
            f"Hallo,\n\ndu wurdest zu „{tenant_name}“ eingeladen.\n"
            f"Einladung annehmen (7 Tage gültig):\n{link}\n"
        ),
    )


def case_confirm_mail(to: str, name: str, link: str) -> Mail:
    return Mail(
        to=to,
        subject="Bitte bestätige deine Anfrage (Myboxi-Gehäuse)",
        body=(
            f"Hallo {name},\n\n"
            "danke für deine Anfrage nach einem gedruckten Myboxi-Gehäuse.\n"
            f"Bitte bestätige sie innerhalb von 48 Stunden:\n{link}\n\n"
            "Ohne Bestätigung löschen wir die Anfrage nach 48 Stunden.\n"
            "Du hast nichts angefragt? Dann ignoriere diese Mail einfach.\n"
        ),
    )


def case_receipt_mail(to: str, name: str) -> Mail:
    return Mail(
        to=to,
        subject="Deine Anfrage ist da (Myboxi-Gehäuse)",
        body=(
            f"Hallo {name},\n\n"
            "deine Anfrage ist bestätigt. Wir melden uns mit einem Angebot: Preis, Versand und\n"
            "Lieferzeit. Erst wenn du das Angebot annimmst, kommt eine Bestellung zustande.\n"
        ),
    )


def case_notify_mail(
    to: str, reply_to: str, lines: list[tuple[str, str]], page_url: str, download_url: str
) -> Mail:
    details = "\n".join(f"{label}: {value}" for label, value in lines)
    return Mail(
        to=to,
        subject="Neue Gehäuse-Anfrage (Myboxi)",
        reply_to=reply_to,
        body=(
            f"Eine Gehäuse-Anfrage ist bestätigt.\n\n{details}\n\n"
            f"Konfiguration: {page_url}\nDruckdateien: {download_url}\n\n"
            "Antworten geht direkt an die anfragende Person.\n"
        ),
    )
