# Myboxi

Offline-first Audiobox für Kleinkinder: NFC-Figur auflegen, Inhalt spielt.
Raspberry Pi als Box, optionaler Server mit Mandanten für die zentrale Verwaltung.

- Spezifikation (Vertrag zwischen Box und Server): [`docs/SPEC.md`](docs/SPEC.md)
- Arbeitsregeln und Stack: [`CLAUDE.md`](CLAUDE.md)
- Server selbst betreiben (Docker Compose oder Debian 13): [`docs/selbst-hosten.md`](docs/selbst-hosten.md)

## Lizenz

| Teil | Lizenz |
|---|---|
| Server (`server/`), Werkzeuge (`tools/`), Deployment (`deploy/`) | [AGPL-3.0-or-later](LICENSES/AGPL-3.0-or-later.txt) |
| Box-Agent (`agent/`) | GPL-3.0-or-later |
| Protokollmodelle (`packages/protocol/`) | [Apache-2.0](LICENSES/Apache-2.0.txt) |
| Spezifikation und Doku (`docs/`, README) | [CC BY 4.0](LICENSES/CC-BY-4.0.txt) |

Die Zuordnung pro Datei steht in [`REUSE.toml`](REUSE.toml) ([REUSE-Standard](https://reuse.software)), die Lizenztexte in [`LICENSES/`](LICENSES/).
Eingebundene Fremddateien: htmx (0BSD), procrastinate-Schema (MIT).
