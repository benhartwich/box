# CLAUDE.md — Projekt Box

Offline-first Audiobox für Kleinkinder (Toniebox-Prinzip): NFC-Figur auflegen → Inhalt spielt. Raspberry Pi als Box, optionaler Server mit Mandanten für zentrale Verwaltung. Open Source.

## Verbindlicher Vertrag

**`docs/SPEC.md` ist die Quelle der Wahrheit** für Datenmodell, Sync, MQTT, HTTP-API und Box-Verhalten.

- Lies die relevanten Spec-Abschnitte, bevor du etwas an Protokoll, Datenmodell oder Box-Verhalten baust.
- Weicht der Code von der Spec ab oder ist die Spec lückenhaft: **nicht still entscheiden.** Stopp, beschreib die Lücke, schlag eine Spec-Änderung vor und warte auf Freigabe.
- Referenziere Spec-Abschnitte in Docstrings und Tests (`# SPEC §5.3`).
- Die Protokollversion (`v1`) wird nie ohne ausdrückliche Anweisung geändert.

## Stack (festgelegt)

### Plattform
| | Server | Box |
|---|---|---|
| OS | Debian 13 (Trixie) | Raspberry Pi OS Lite 64-bit (Trixie-Basis) |
| Python | 3.13 aus Debian (System-Interpreter), Venv per `uv` | 3.13 aus Raspberry Pi OS, Venv per `uv` |
| Prozesse | systemd | systemd |

Pakete aus den Debian-Repos haben Vorrang vor Fremdquellen. Kein Docker für den eigenen Betrieb in v1.

### Gemeinsam
- `uv`-Workspace, `ruff` (lint + format), `pyright` strict, `pytest` + `pytest-asyncio`
- `packages/protocol`: Pydantic-v2-Modelle, von Agent **und** Server genutzt
- Konfiguration: `pydantic-settings`, Env-Dateien unter `/etc/box-*/`
- Logging: strukturiert (JSON) nach journald, mit Secret-Filter

### Server
| Bereich | Wahl |
|---|---|
| Web | FastAPI, uvicorn auf Unix-Socket hinter **nginx** (TLS via certbot) |
| Datenbank | PostgreSQL 17 (Debian), SQLAlchemy 2 async + `asyncpg`, Alembic |
| Hintergrundjobs | `procrastinate` (Postgres-basiert, kein Redis) — Transkodierung, Aufräumen |
| Medien | `ffmpeg` (Debian) → Opus + EBU-R128-Loudnorm |
| Asset-Store | Dateisystem, content-adressiert; Auslieferung per nginx `X-Accel-Redirect` (SPEC §3.8) |
| MQTT | Mosquitto 2 (Debian) mit Dynamic-Security-Plugin, TLS auf 8883; optional (SPEC §6) |
| Web-UI | Jinja2 + HTMX, minimales Vanilla-JS (u. a. MediaRecorder für Aufnahmen). **Kein Node-Build.** |
| Nutzer-Auth | Argon2id (`argon2-cffi`), serverseitige Sessions in Postgres, Einladungen und Verifizierung per SMTP |

### Box-Agent
| Bereich | Wahl |
|---|---|
| Laufzeit | asyncio, SQLite (WAL) |
| Hardware | `gpiozero` (Taster), Adafruit PN532 via Blinka (I2C) |
| Audio | PipeWire (von Soloist vorausgesetzt), `mpv` über JSON-IPC |
| Netzwerk | `httpx`, `aiomqtt`, `websockets` (Soloist, nur `127.0.0.1`) |
| Verteilung | ab M5 als `.deb` mit gebündeltem Venv über eigenes apt-Repo |

Neue Abhängigkeiten nur mit Begründung. Auf dem Agent zählt jedes MB (Zielhardware: Pi Zero 2 W, 512 MB RAM).

## Repo-Layout

```
docs/SPEC.md            Vertrag
packages/protocol/      Pydantic-Modelle: Envelope, Delta, reported, events, cmd
agent/                  Box-Agent
  src/box_agent/
    core/               Reine Logik: Playback-Zustandsautomat, Lautstärke-Policy, Resume, Sync-Anwendung
    adapters/           Hardware & I/O hinter Interfaces: reader, buttons, player, battery, clock, announcer
    store/              SQLite-Schema und Repositories
    providers/          local, podcast, spotify, stream (SPEC §8)
    sync/               HTTPS-Client, MQTT, Outbox
  tests/
server/                 FastAPI-Server (ab M1)
  src/box_server/
    api/device/         Geräte-API (SPEC §7)
    api/web/            Web-UI (Jinja2 + HTMX)
    domain/             Mandanten, Figuren, Inhalte, Bindings, Revisionen
    storage/            Asset-Store-Schnittstelle + Dateisystem-Backend
    jobs/               procrastinate-Tasks
    mqtt/               Dynsec-Provisionierung, notify/cmd
  migrations/           Alembic
tools/mock-server/      Statischer Mock der Geräte-API aus SPEC §7
fixtures/               Beispiel-Bibliotheken, Test-Audio (kurz, CC0)
deploy/                 systemd-Units, Mosquitto-Config, docker-compose
image/                  Pi-Image-Build (ab M5)
```

## Befehle

```bash
uv sync                                  # alles installieren
uv run pytest                            # alle Tests
uv run pytest agent                      # nur Agent
uv run ruff check . && uv run ruff format --check .
uv run pyright
uv run box-agent --sim                   # Agent mit simulierter Hardware am Laptop
uv run box-agent sim place 04A2B3C4D5E680
uv run box-agent sim remove
uv run box-agent sim press volume_up
uv run pytest server                     # braucht lokales PostgreSQL, siehe server/README.md
uv run alembic -c server/alembic.ini upgrade head
uv run box-server dev                    # Dev-Server ohne nginx
uv run box-server worker                 # Job-Worker
```

Vor jedem Commit: Tests, ruff und pyright grün.

## Architekturregeln

1. **Offline-first.** Der Agent darf nie auf Netzwerk warten, um eine gebundene lokale Figur abzuspielen. Netzwerkfehler dürfen die Wiedergabe nicht blockieren.
2. **Hardware nur hinter Interfaces.** `core/` importiert nie `gpiozero`, `board`, `mpv` oder Netzwerkbibliotheken. Jeder Adapter hat eine Sim-Variante; alles läuft am Laptop mit `--sim`.
3. **Zeit ist injiziert.** Kein direktes `datetime.now()` / `time.time()` in `core/`. Uhr kommt über das `Clock`-Interface inkl. `time_trusted` (SPEC §5.6).
4. **Lautstärke an genau einer Stelle begrenzt.** Die Volume-Policy ist die einzige Funktion, die eine effektive Lautstärke berechnet. Tasten, Kommandos und Soloist gehen alle durch sie (SPEC §9.2).
5. **Stromausfall ist Normalbetrieb.** Kind zieht den Akku. SQLite im WAL-Modus, Resume-Position mindestens alle 10 s und bei jeder Zustandsänderung sichern. Kein Zustand, der nur im RAM lebt und wichtig ist.
6. **Jede Situation ist hörbar.** Fehler, unbekannte Figur, "wird geladen": immer ein definierter Ton oder eine Ansage, nie Stille (SPEC §1.7).
7. **Mandanten-Isolation** (Server): jede Query auf mandantenbezogene Tabellen filtert nach `tenant_id`. Tests für mandantenübergreifenden Zugriff sind Pflicht.
8. **Nachrichten nur über `packages/protocol`.** Keine handgebauten Dicts für Protokollnachrichten.
9. **`packages/protocol` hat einen Besitzer pro Änderung.** Agent- und Server-Session ändern es nie gleichzeitig. Änderungen daran sind immer auch Spec-Änderungen.
10. **Revisionen transaktional.** Jede Änderung an Figuren, Inhalten oder Bindings erhöht `tenant.config_rev` in derselben DB-Transaktion (SPEC §5.1).

## Sicherheit — harte Regeln

- **Niemals** Soloist-Binaries oder -Archive ins Repo, Image oder in Fixtures. Soloist wird zur Laufzeit von der offiziellen Spotify-Quelle geladen.
- **Niemals** Soloist-API-Key oder Device-Secret loggen, in Sync-Payloads schreiben oder in Crash-Reports aufnehmen. Logger-Filter dafür ist Teil von M0.
- **Kein Spotify Web API.** Der Spotify-Provider nutzt ausschließlich die lokale Soloist-WebSocket-API auf `127.0.0.1`.
- Kein Dienst des Agents lauscht auf `0.0.0.0` außerhalb des Setup-Modus (SPEC §9.3).
- Kein Mikrofon-Code vor ausdrücklicher Freigabe von v2.
- Server: uvicorn nur auf Unix-Socket, nie öffentlich. Asset-Dateien nie direkt von nginx ohne vorherige Berechtigungsprüfung (`internal`-Location).

## Tests

- Jede Verhaltensregel aus SPEC §9 hat mindestens einen Test in `core/`, mit Fake-Clock und Sim-Adaptern.
- Protokollmodelle haben Round-Trip-Tests gegen die JSON-Beispiele aus der Spec.
- Test-Audio: kurze, generierte Sinus-/Stille-Dateien oder CC0. Keine urheberrechtlich geschützten Inhalte im Repo.

## Konventionen

- Code, Identifier, Kommentare, Commit-Messages: Englisch. Doku unter `docs/`: Deutsch.
- Commits: Conventional Commits (`feat(agent): …`, `fix(protocol): …`).
- Kleine, reviewbare Schritte. Nach jedem abgeschlossenen Schritt kurz zusammenfassen, was fertig ist und was als Nächstes kommt.

## Zielhardware

- Referenz: Raspberry Pi Zero 2 W, PN532 (I2C), 4 Taster (`play_pause`, `volume_up`, `volume_down`, `next`), USB-C-Powerbank.
- Entwicklung: Raspberry Pi 4 und Laptop im Sim-Modus.
- Keine Echtzeituhr annehmen.

## Aktueller Stand

Agent: **M0** · Server: **M1** (siehe SPEC §12). Offene Punkte: SPEC §13. Spec-Version: v0.3.
