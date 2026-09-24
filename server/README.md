# Box-Server

FastAPI-Server für Box (SPEC v0.3): Mandanten, Nutzer, Bibliothek, Geräte-API.
Betrieb auf Debian 13: siehe `docs/betrieb-debian13.md`.

## Entwicklung

Voraussetzungen: Python 3.13 (System), [uv](https://docs.astral.sh/uv/), ffmpeg, Docker (nur für die lokale Datenbank).

```bash
tools/dev-postgres.sh        # PostgreSQL 17 in Docker auf 127.0.0.1:55433, schreibt .dev/env
source .dev/env
uv sync
uv run box-server migrate    # Schema anwenden
uv run box-server dev        # http://127.0.0.1:8000
uv run box-server worker     # Job-Worker (Transkodierung, Aufräumen)
```

`.dev/` ist git-ignoriert und enthält das lokale DB-Passwort und den JWT-Schlüssel.

## Tests

```bash
source .dev/env
uv run pytest server
```

Die Tests nutzen die Datenbank aus `BOX_SERVER_TEST_DATABASE_URL` und setzen dort pro Testlauf
das Schema `public` neu auf. Niemals auf eine produktive Datenbank zeigen lassen.

## Konfiguration

Alle Einstellungen kommen aus Umgebungsvariablen mit Präfix `BOX_SERVER_`
(siehe `src/box_server/settings.py` und `deploy/box-server.env.example`).
