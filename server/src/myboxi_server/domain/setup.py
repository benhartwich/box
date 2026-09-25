"""Setup wizard for a box: every step is derived from what server and box report.

Nothing is stored for the wizard itself. The box's self-test and button test come from
``reported`` (SPEC v0.6 §6.4, §9.6); pairing, figures, bindings and events from the database.
"""

from __future__ import annotations

import datetime as dt
import hashlib
from dataclasses import dataclass, field
from typing import Literal

from pydantic import ValidationError
from sqlalchemy import exists, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from myboxi_protocol.reported import ReportedData
from myboxi_server.domain.authz import Perm, TenantContext
from myboxi_server.models import Binding, Content, ContentItem, Device, Event, Tenant, Token, Upload
from myboxi_server.models.enums import ContentKind, UploadStatus

StepStatus = Literal["done", "active", "waiting", "problem", "skipped"]
HintLevel = Literal["ok", "warn", "fail"]

DELIVERY_TTL = dt.timedelta(minutes=10)  # domain/pairing.py: a claimed code must be fetched
BUTTONS = [
    ("play_pause", "Play/Pause"),
    ("volume_up", "Lauter"),
    ("volume_down", "Leiser"),
    ("next", "Weiter"),
]
CHECK_LABELS = {
    "nfc": "NFC-Leser",
    "audio": "Lautsprecher",
    "buttons": "Taster",
    "prompts": "Ansagen",
}
HEALTH_TEXT = {
    ("nfc", "no_i2c"): "Der I2C-Bus fehlt. Spiel das Image neu auf die SD-Karte.",
    ("nfc", "not_responding"): "Der NFC-Leser antwortet nicht. Prüfe die Kabel (SDA an Pin 3, "
    "SCL an Pin 5, VCC an 3,3 V, GND) und ob der DIP-Schalter auf I2C steht.",
    ("nfc", "read_error"): "Der NFC-Leser hatte Lesefehler und wurde neu gestartet. "
    "Vielleicht ein Wackelkontakt.",
    ("audio", "player_down"): "Die Wiedergabe läuft nicht. "
    "Starte die Box neu (Stecker kurz ziehen).",
    ("audio", "no_output"): "Kein Audioausgang gefunden. Prüfe den Verstärker: BCLK an Pin 12, "
    "LRC an Pin 35, DIN an Pin 40, VIN an 5 V.",
    ("buttons", "gpio_error"): "Die Taster lassen sich nicht einrichten. Starte die Box neu.",
    ("prompts", "missing"): "Im Image fehlen Ansagen. Spiel das Image neu auf die SD-Karte.",
}
WEAK_WIFI_DBM = -75
LOW_STORAGE_MB = 500


@dataclass(frozen=True)
class Hint:
    level: HintLevel
    text: str


@dataclass
class Step:
    key: str
    title: str
    status: StepStatus = "waiting"
    text: str = ""
    hints: list[Hint] = field(default_factory=list[Hint])

    def set(self, status: StepStatus, text: str = "") -> None:
        self.status = status
        if text:
            self.text = text


@dataclass
class SetupProgress:
    device: Device
    steps: list[Step]
    reported: ReportedData | None = None
    buttons: list[tuple[str, str, bool]] = field(default_factory=list[tuple[str, str, bool]])
    unknown_uid: str | None = None
    token: Token | None = None
    contents: list[Content] = field(default_factory=list[Content])

    @property
    def done(self) -> bool:
        return all(s.status in ("done", "skipped") for s in self.steps)

    @property
    def completed(self) -> int:
        return sum(s.status in ("done", "skipped") for s in self.steps)

    @property
    def key(self) -> str:
        """Changes whenever the page would look different (HTMX polling swaps only then)."""
        parts = [f"{s.key}:{s.status}:{s.text}:{[h.text for h in s.hints]}" for s in self.steps]
        parts += [str(self.buttons), str(self.unknown_uid), str(self.token and self.token.id)]
        parts += [str([c.id for c in self.contents])]
        return hashlib.sha256("|".join(parts).encode()).hexdigest()[:16]


def reported_data(device: Device) -> ReportedData | None:
    if not device.reported:
        return None
    try:
        return ReportedData.model_validate(device.reported)
    except ValidationError:
        return None


def health_hints(data: ReportedData | None) -> list[Hint]:
    """Self-test results in words, for the wizard and the box page."""
    if data is None or data.health is None:
        return []
    hints: list[Hint] = []
    for h in data.health:
        label = CHECK_LABELS.get(h.check, h.check)
        if h.level == "ok":
            hints.append(Hint("ok", f"{label}: in Ordnung"))
        else:
            text = HEALTH_TEXT.get((h.check, h.code), f"Problem ({h.code}).")
            hints.append(Hint(h.level, f"{label}: {text}"))
    return hints


def _report_hints(data: ReportedData) -> list[Hint]:
    version = f"Version {data.agent_version}"
    if data.image_version:
        version += f", Image {data.image_version}"
    hints = [Hint("ok", version)]
    if data.wifi_rssi is not None:
        if data.wifi_rssi < WEAK_WIFI_DBM:
            text = f"WLAN-Signal schwach ({data.wifi_rssi} dBm). Stell die Box näher an den Router."
            hints.append(Hint("warn", text))
        else:
            hints.append(Hint("ok", f"WLAN-Signal gut ({data.wifi_rssi} dBm)"))
    if data.storage.free_mb < LOW_STORAGE_MB:
        hints.append(Hint("warn", f"Nur noch {data.storage.free_mb} MB frei auf der SD-Karte."))
    if not data.time_trusted:
        text = (
            "Die Uhrzeit ist noch nicht synchron. Bis dahin gelten Ruhezeiten "
            "nur als Lautstärkegrenze."
        )
        hints.append(Hint("warn", text))
    return hints


async def _latest_unknown_uids(
    db: AsyncSession, ctx: TenantContext, device: Device, since: dt.datetime
) -> list[str]:
    uid = Event.data["uid"].astext
    rows = await db.scalars(
        select(uid)
        .where(
            Event.device_id == device.id,
            Event.tenant_id == ctx.tenant_id,
            Event.type == "token_unknown",
            Event.received_at >= since,
        )
        .order_by(Event.received_at.desc())
        .limit(20)
    )
    return [u for u in rows if u]


async def setup_progress(db: AsyncSession, ctx: TenantContext, device: Device) -> SetupProgress:
    """Eight steps from pairing to the first figure played on this box."""
    ctx.require(Perm.READ)
    now = dt.datetime.now(dt.UTC)
    since = device.paired_at or now
    data = reported_data(device)
    steps = [
        Step("credentials", "Box holt ihre Zugangsdaten ab"),
        Step("report", "Box meldet sich"),
        Step("selftest", "Selbsttest"),
        Step("buttons", "Tastentest"),
        Step("figure", "Erste Figur"),
        Step("content", "Inhalt zuordnen"),
        Step("sync", "Box lädt den Inhalt"),
        Step("played", "Abspielen"),
    ]
    by_key = {s.key: s for s in steps}
    progress = SetupProgress(device=device, steps=steps, reported=data)

    # 1. credentials: the box polled after the claim (domain/pairing.py stores the secret).
    s = by_key["credentials"]
    if device.secret_hash is not None:
        s.status = "done"
    elif now - since > DELIVERY_TTL:
        s.set(
            "problem",
            "Die Box hat ihre Zugangsdaten nicht abgeholt, der Code ist verfallen. Halte an "
            "der Box „Play/Pause“ und „Weiter“ 5 Sekunden lang und gib den neuen Code ein.",
        )
    else:
        s.text = "Die Box sollte in wenigen Sekunden „Geschafft!“ sagen."

    # 2. first report after this pairing.
    s = by_key["report"]
    if data is not None and device.reported_at is not None and device.reported_at >= since:
        s.set("done")
        s.hints = _report_hints(data)
    else:
        s.text = "Die Box schickt nach der Kopplung ihren Zustand."

    # 3. self-test (SPEC v0.6 §6.4 health).
    s = by_key["selftest"]
    if data is None:
        pass
    elif data.health is None:
        s.set(
            "skipped",
            "Diese Box meldet noch keinen Selbsttest. Spiel ein neueres Image auf "
            "(ab Version 0.2), dann erscheint er hier.",
        )
    else:
        s.hints = health_hints(data)
        s.status = "problem" if any(h.level == "fail" for h in s.hints) else "done"

    # 4. button test (SPEC v0.6 §9.6, only during the setup phase).
    s = by_key["buttons"]
    if data is not None and data.button_test is not None:
        seen = set(data.button_test.seen)
        progress.buttons = [(name, label, name in seen) for name, label in BUTTONS]
        if all(name in seen for name, _ in BUTTONS):
            s.status = "done"
        else:
            s.text = "Drück jede Taste einmal. Die Box piept bei jedem Druck."
    elif data is not None and data.health is not None:
        s.set("skipped", "Der Tastentest läuft nur in der ersten Stunde nach dem Koppeln.")
    elif data is not None:
        s.set("skipped", "Diese Box kennt den Tastentest noch nicht (Image ab Version 0.2).")

    # 5. first figure: any figure in the household (a second box has one already).
    s = by_key["figure"]
    unknown = await _latest_unknown_uids(db, ctx, device, since)
    known: set[str] = set()
    if unknown:
        known = set(
            await db.scalars(
                select(Token.uid).where(Token.tenant_id == ctx.tenant_id, Token.uid.in_(unknown))
            )
        )
    progress.unknown_uid = next((u for u in unknown if u not in known), None)
    token: Token | None = None
    adopted = next((u for u in unknown if u in known), None)
    if adopted is not None:
        token = await db.scalar(
            select(Token).where(Token.tenant_id == ctx.tenant_id, Token.uid == adopted)
        )
    if token is None:
        token = await db.scalar(
            select(Token)
            .where(Token.tenant_id == ctx.tenant_id)
            .order_by(Token.created_at.desc())
            .limit(1)
        )
    progress.token = token
    if token is not None:
        s.set("done", f"„{token.label}“")
    elif progress.unknown_uid is not None:
        s.text = "Die Box hat eine neue Figur erkannt. Gib ihr einen Namen."
    else:
        s.text = (
            "Leg eine neue Figur auf die Box. Sie sagt „Diese Figur kenne ich noch nicht“, "
            "dann erscheint die Figur hier."
        )

    # 6. content bound to that figure, with finished tracks.
    s = by_key["content"]
    binding = (
        await db.scalar(
            select(Binding).where(Binding.tenant_id == ctx.tenant_id, Binding.token_id == token.id)
        )
        if token is not None
        else None
    )
    if token is not None and binding is None:
        progress.contents = list(
            await db.scalars(
                select(Content)
                .where(Content.tenant_id == ctx.tenant_id)
                .order_by(func.lower(Content.title))
            )
        )
        s.text = (
            "Wähle, was die Figur abspielen soll."
            if progress.contents
            else "Leg zuerst einen Inhalt an und lade Hörspiele oder Musik hoch."
        )
    elif binding is not None:
        content = await db.scalar(
            select(Content).where(
                Content.tenant_id == ctx.tenant_id, Content.id == binding.content_id
            )
        )
        items = await db.scalar(
            select(func.count())
            .select_from(ContentItem)
            .where(
                ContentItem.tenant_id == ctx.tenant_id,
                ContentItem.content_id == binding.content_id,
            )
        )
        converting = await db.scalar(
            select(
                exists().where(
                    Upload.tenant_id == ctx.tenant_id,
                    Upload.content_id == binding.content_id,
                    Upload.status.in_([UploadStatus.PENDING, UploadStatus.PROCESSING]),
                )
            )
        )
        title = content.title if content else "Inhalt"
        if content is not None and content.kind != ContentKind.COLLECTION:
            s.set(
                "problem",
                f"„{title}“ ist ein Podcast, Stream oder Spotify-Inhalt. Das kann die Box "
                "noch nicht abspielen. Wähle eigene Dateien.",
            )
        elif items:
            s.set("done", f"„{title}“")
        elif converting:
            s.set("active", f"„{title}“ wird gerade umgewandelt …")
        else:
            s.set("problem", f"„{title}“ hat noch keine Titel. Lade Dateien hoch.")

    # 7. the box applied the current library (SPEC §5.3: only after all downloads).
    s = by_key["sync"]
    if by_key["content"].status == "done" and data is not None:
        config_rev = await db.scalar(select(Tenant.config_rev).where(Tenant.id == ctx.tenant_id))
        if data.applied_config_rev >= (config_rev or 0):
            s.status = "done"
        else:
            s.text = "Die Box lädt die Dateien herunter. Das dauert je nach Größe etwas."

    # 8. a figure was played on this box since pairing.
    s = by_key["played"]
    played = await db.scalar(
        select(
            exists().where(
                Event.device_id == device.id,
                Event.tenant_id == ctx.tenant_id,
                Event.type == "token_played",
                Event.received_at >= since,
            )
        )
    )
    if played:
        s.status = "done"
    elif by_key["sync"].status == "done":
        s.text = "Leg die Figur noch einmal auf die Box. Sie spielt jetzt ab."

    # The first unfinished step is the one to work on.
    for step in steps:
        if step.status in ("waiting", "active"):
            step.status = "active"
            break
    return progress
