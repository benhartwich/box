# Myboxi-Server

FastAPI-Server für Myboxi (SPEC v0.4): Mandanten, Nutzer, Bibliothek, Geräte-API.
Betrieb auf Debian 13: siehe `docs/betrieb-debian13.md`.

## Entwicklung

Voraussetzungen: Python 3.13 (System), [uv](https://docs.astral.sh/uv/), ffmpeg, Docker (nur für die lokale Datenbank).

```bash
tools/dev-postgres.sh        # PostgreSQL 17 in Docker auf 127.0.0.1:55433, schreibt .dev/env
source .dev/env
uv sync
uv run myboxi-server migrate    # Schema anwenden
uv run myboxi-server dev        # http://127.0.0.1:8000
uv run myboxi-server worker     # Job-Worker (Transkodierung, Aufräumen)
```

`.dev/` ist git-ignoriert und enthält das lokale DB-Passwort und den JWT-Schlüssel.

## Tests

```bash
source .dev/env
uv run pytest server
```

Die Tests nutzen die Datenbank aus `MYBOXI_SERVER_TEST_DATABASE_URL` und setzen dort pro Testlauf
das Schema `public` neu auf. Niemals auf eine produktive Datenbank zeigen lassen.

## Konfiguration

Alle Einstellungen kommen aus Umgebungsvariablen mit Präfix `MYBOXI_SERVER_`
(siehe `src/myboxi_server/settings.py` und `deploy/myboxi-server.env.example`).

## Deployment prüfen

```bash
tools/debian13-check/run.sh          # frisches Debian 13 in Docker, Installation nach docs/betrieb-debian13.md
```

Das Skript führt die Shell-Blöcke der Betriebsanleitung in einem Container mit systemd aus. Statt certbot kommt ein selbstsigniertes Zertifikat zum Einsatz. Danach prüft es HTTPS, Upload und Transkodierung durch die Worker-Unit, den kompletten Geräteablauf über nginx/`X-Accel-Redirect` und dass die Logs keine Secrets enthalten. Es lauscht nur auf `127.0.0.1:18443`.
