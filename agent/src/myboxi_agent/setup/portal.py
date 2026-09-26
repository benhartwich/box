"""Captive portal of the setup mode (SPEC §9.3): a deliberately tiny HTTP/1.1 server.

It only listens on the access point address while setup mode runs. All input is validated
and every value that reaches HTML is escaped.
"""

from __future__ import annotations

import asyncio
import contextlib
import html
import logging
import time
from collections.abc import Sequence
from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from myboxi_agent.setup.nm import HOTSPOT_ADDRESS, Network
from myboxi_agent.sync.tls import MAX_CA_PEM, normalize_ca, valid_ca

log = logging.getLogger(__name__)

MAX_HEADER = 8192
MAX_BODY = 16384  # the form with an own CA certificate (SPEC v0.13 §9.3)
PORTAL = f"http://{HOTSPOT_ADDRESS}/"
# Captive portal probes (Android, ChromeOS, Windows); Apple gets the page itself.
REDIRECT_PROBES = {"/generate_204", "/gen_204", "/connecttest.txt", "/ncsi.txt", "/redirect"}
PAGE_PROBES = {"/hotspot-detect.html", "/library/test/success.html", "/success.txt"}


@dataclass(frozen=True)
class Submission:
    ssid: str
    password: str
    server_url: str
    # SPEC v0.9 §9.3: None keeps the stored key; never shown again, never logged.
    soloist_key: str | None = None
    soloist_clear: bool = False
    # SPEC v0.13 §9.3: own CA of a self-hosted server; None keeps the stored one.
    server_ca: str | None = None
    server_ca_clear: bool = False

    def __repr__(self) -> str:  # no password or key in any log line
        return f"Submission(ssid={self.ssid!r}, server_url={self.server_url!r})"


class InvalidInput(ValueError):
    pass


def validate(form: dict[str, str]) -> Submission:
    ssid = (form.get("ssid_manual") or form.get("ssid") or "").strip()
    if not 1 <= len(ssid.encode()) <= 32:
        raise InvalidInput("Bitte ein WLAN auswählen oder eingeben.")
    password = form.get("password", "")
    if password and not (8 <= len(password) <= 63 and password.isprintable()):
        raise InvalidInput("Das WLAN-Passwort muss 8 bis 63 Zeichen haben.")
    url = form.get("server_url", "").strip()
    parts = urlsplit(url)
    # SPEC v0.12 §9.3: HTTPS only, the box's credentials must never cross the network in
    # plain text.
    if parts.scheme != "https" or not parts.netloc or len(url) > 200:
        raise InvalidInput("Bitte eine Server-Adresse wie https://app.myboxi.eu angeben.")
    key = form.get("soloist_key", "").strip() or None
    if key is not None and not (8 <= len(key) <= 512 and all(33 <= ord(c) <= 126 for c in key)):
        raise InvalidInput(
            "Der Spotify-Schlüssel sieht nicht richtig aus. Bitte aus dem Spotify-Entwicklerportal "
            "kopieren."
        )
    clear = form.get("soloist_clear") == "1"
    ca = form.get("server_ca", "").strip() or None
    if ca is not None and not valid_ca(ca):
        raise InvalidInput(
            "Das CA-Zertifikat sieht nicht richtig aus. Bitte die Datei myboxi-ca.pem "
            "vollständig einfügen, mit -----BEGIN CERTIFICATE-----."
        )
    ca_clear = form.get("server_ca_clear") == "1"
    return Submission(
        ssid, password, url.rstrip("/"), None if clear else key, clear,
        None if ca_clear or ca is None else normalize_ca(ca), ca_clear,
    )  # fmt: skip


STYLE = (
    "body{font:17px/1.5 system-ui,sans-serif;margin:0;padding:1rem;background:#fbfaf7;"
    "color:#1f2328}main{max-width:28rem;margin:auto}label{display:block;margin:.8rem 0 .2rem;"
    "font-weight:600}input,select,button{font:inherit;width:100%;box-sizing:border-box;"
    "padding:.6rem;border:1px solid #ccc;border-radius:10px}button{margin-top:1.2rem;"
    "background:#2f6f5e;color:#fff;border:0}.err{background:#fbe9e7;padding:.6rem;"
    "border-radius:10px}.muted{color:#6a737d;font-size:.9em}"
    ".check{display:flex;gap:.5rem;align-items:center;font-weight:400}.check input{width:auto}"
    "details{margin-top:1rem}summary{font-weight:600}"
)


def page(body: str, title: str = "Myboxi einrichten") -> bytes:
    return (
        '<!doctype html><html lang="de"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{html.escape(title)}</title><style>{STYLE}</style></head>"
        f"<body><main>{body}</main></body></html>"
    ).encode()


def spotify_fields(key_set: bool) -> str:
    """SPEC v0.9 §9.3: optional Soloist key; the page never shows a stored key."""
    state = (
        "Ein Schlüssel ist gespeichert. Leer lassen, um ihn zu behalten."
        if key_set
        else "Nur für Spotify nötig: Spotify Premium und ein eigener Schlüssel aus dem Spotify-"
        "Entwicklerportal (Spotify Soloist API Key)."
    )
    clear = (
        '<label class="check"><input type="checkbox" name="soloist_clear" value="1">'
        "Schlüssel löschen</label>"
        if key_set
        else ""
    )
    return (
        f"<details{' open' if key_set else ''}><summary>Spotify (optional)</summary>"
        '<label for="soloist_key">Spotify-Schlüssel</label>'
        '<input id="soloist_key" name="soloist_key" type="password" maxlength="512" '
        'autocomplete="off" spellcheck="false">'
        f'<p class="muted">{html.escape(state)}</p>{clear}</details>'
    )


def server_ca_fields(ca_set: bool) -> str:
    """SPEC v0.13 §9.3: only for a self-hosted server without a public certificate."""
    state = (
        "Ein Zertifikat ist gespeichert. Leer lassen, um es zu behalten."
        if ca_set
        else "Nur für einen eigenen Server mit eigener Zertifizierungsstelle, z. B. nur im "
        "Heimnetz. Den Inhalt von myboxi-ca.pem hier einfügen."
    )
    clear = (
        '<label class="check"><input type="checkbox" name="server_ca_clear" value="1">'
        "Zertifikat entfernen</label>"
        if ca_set
        else ""
    )
    return (
        f"<details{' open' if ca_set else ''}><summary>Eigenes Zertifikat (optional)</summary>"
        '<label for="server_ca">CA-Zertifikat (PEM)</label>'
        f'<textarea id="server_ca" name="server_ca" rows="5" maxlength="{MAX_CA_PEM}" '
        'autocomplete="off" spellcheck="false" style="width:100%;font:12px monospace">'
        "</textarea>"
        f'<p class="muted">{html.escape(state)}</p>{clear}</details>'
    )


def form_page(
    networks: Sequence[Network],
    server_url: str,
    error: str | None,
    key_set: bool = False,
    ca_set: bool = False,
) -> bytes:
    options = "".join(
        f'<option value="{html.escape(n.ssid, quote=True)}">'
        f"{html.escape(n.ssid)}{' 🔒' if n.secured else ''}</option>"
        for n in networks
    )
    err = f'<p class="err" role="alert">{html.escape(error)}</p>' if error else ""
    return page(
        f"<h1>Myboxi einrichten</h1>{err}"
        '<form method="post" action="/connect">'
        f'<label for="ssid">WLAN</label><select id="ssid" name="ssid">{options}</select>'
        '<label for="ssid_manual">oder WLAN-Name eingeben</label>'
        '<input id="ssid_manual" name="ssid_manual" maxlength="32" autocomplete="off">'
        '<label for="password">WLAN-Passwort</label>'
        '<input id="password" name="password" type="password" maxlength="63">'
        '<label for="server_url">Server</label>'
        f'<input id="server_url" name="server_url" value="{html.escape(server_url, quote=True)}">'
        '<p class="muted">Nur ändern, wenn du einen eigenen Myboxi-Server betreibst.</p>'
        f"{server_ca_fields(ca_set)}"
        f"{spotify_fields(key_set)}"
        "<button type=submit>Verbinden</button></form>"
    )


def connecting_page(ssid: str) -> bytes:
    return page(
        f"<h1>Verbinde mit {html.escape(ssid)} …</h1>"
        "<p>Die Box trennt jetzt dieses WLAN und sagt an, ob es geklappt hat. "
        "Danach kannst du sie in der Myboxi-App hinzufügen.</p>"
    )


class Portal:
    def __init__(
        self,
        networks: Sequence[Network],
        default_server_url: str,
        error: str | None = None,
        key_set: bool = False,
        ca_set: bool = False,
    ) -> None:
        self.networks = list(networks)
        self.default_server_url = default_server_url
        self.error = error
        self.key_set = key_set
        self.ca_set = ca_set
        self.last_activity = time.monotonic()
        self.submission: asyncio.Future[Submission] | None = None

    async def serve(self, host: str, port: int) -> asyncio.Server:
        self.submission = asyncio.get_running_loop().create_future()
        return await asyncio.start_server(self._client, host, port)

    async def _client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.last_activity = time.monotonic()
        try:
            status, headers, body = await self._handle(reader)
        except (ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, TimeoutError):
            status, headers, body = 400, {}, b"bad request"
        reason = {200: "OK", 302: "Found"}.get(status, "Bad Request")
        head = f"HTTP/1.1 {status} {reason}\r\n"
        headers = {
            "Content-Length": str(len(body)),
            "Connection": "close",
            "Cache-Control": "no-store",
        } | headers
        head += "".join(f"{k}: {v}\r\n" for k, v in headers.items()) + "\r\n"
        with contextlib.suppress(ConnectionError):
            writer.write(head.encode() + body)
            await writer.drain()
        writer.close()

    async def _handle(self, reader: asyncio.StreamReader) -> tuple[int, dict[str, str], bytes]:
        raw = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), 10)
        if len(raw) > MAX_HEADER:
            raise ValueError("header too large")
        lines = raw.decode("latin-1").split("\r\n")
        method, target, _ = lines[0].split(" ", 2)
        fields = {
            k.strip().lower(): v.strip()
            for k, _, v in (line.partition(":") for line in lines[1:] if line)
        }
        path = urlsplit(target).path or "/"
        html_type = {"Content-Type": "text/html; charset=utf-8"}
        if path in REDIRECT_PROBES:
            return 302, {"Location": PORTAL}, b""
        if method == "POST" and path == "/connect":
            length = int(fields.get("content-length", "0"))
            if length > MAX_BODY:
                raise ValueError("body too large")
            body = await asyncio.wait_for(reader.readexactly(length), 10)
            form = {k: v[0] for k, v in parse_qs(body.decode("utf-8", "replace")).items()}
            try:
                submission = validate(form)
            except InvalidInput as exc:
                return (
                    200,
                    html_type,
                    form_page(
                        self.networks,
                        form.get("server_url", ""),
                        str(exc),
                        self.key_set,
                        self.ca_set,
                    ),
                )
            if self.submission is not None and not self.submission.done():
                self.submission.set_result(submission)
            return 200, html_type, connecting_page(submission.ssid)
        if method == "GET" and (path in ("/", "/index.html") or path in PAGE_PROBES):
            return (
                200,
                html_type,
                form_page(
                    self.networks, self.default_server_url, self.error, self.key_set, self.ca_set
                ),
            )
        return 302, {"Location": PORTAL}, b""
