# Software-Updates der Box

Boxen ab Image 0.3.0 aktualisieren sich selbst (SPEC §11.1). Diese Seite beschreibt, wie Updates entstehen und wie die Signaturschlüssel verwaltet werden.

## Ablauf

1. Ein Tag `box-vX.Y.Z` auf `main` startet den Workflow `image`. Er prüft zuerst, ob der Tag zur Agent-Version in `agent/pyproject.toml` passt.
2. Der Build erzeugt das Image und das Update-Paket `myboxi-agent-X.Y.Z-arm64.tar.xz`. Das Paket enthält das Verzeichnis `X.Y.Z/` mit Venv und Ansagen.
3. `tools/release/make_manifest.py` schreibt `manifest.json` mit Version, URL, SHA-256 und Größe des Pakets.
4. Die CI signiert das Manifest mit dem Schlüssel aus dem Secret `MYBOXI_UPDATE_KEY` (Ed25519) und prüft die Signatur gegen den öffentlichen Schlüssel im Repository.
5. Manifest und Signatur landen im Release `channel-stable`. Die Boxen lesen dieselbe URL: `…/releases/download/channel-stable/manifest.json`.

Auf der Box prüft `myboxi-updater.service` drei Minuten nach dem Start, danach stündlich und jedes Mal, wenn die Box wieder online ist. Sie installiert nur mit gültiger Signatur, nur neuere Versionen und nie, solange etwas spielt. Läuft die neue Version nicht, kehrt sie zur alten zurück. Den Stand zeigt die App auf der Box-Seite.

Sicherheitsupdates des Betriebssystems installiert `unattended-upgrades` täglich. Braucht eines einen Neustart, startet die Box neu, sobald sie 10 Minuten lang nichts gespielt hat.

## Schlüssel

| Schlüssel | Öffentlich (im Image) | Privat |
|---|---|---|
| Haupt | `image/files/etc/myboxi-agent/update-keys/myboxi-2026-main.pem` | GitHub-Secret `MYBOXI_UPDATE_KEY` und Backup beim Projektinhaber |
| Notfall | `…/myboxi-2026-backup.pem` | nur als Backup beim Projektinhaber |

Die Box vertraut jedem Schlüssel in `/etc/myboxi-agent/update-keys/`.

- **Hauptschlüssel verloren oder kompromittiert:** Das Secret `MYBOXI_UPDATE_KEY` durch den Notfall-Schlüssel ersetzen und ein Release bauen, das einen neuen Schlüsselsatz ins Image legt. Boxen nehmen neue Schlüssel nur mit einem neuen Image auf, weil der Updater das Agent-Verzeichnis tauscht, nicht `/etc`.
- **Nie** einen privaten Schlüssel ins Repository, in Logs oder in Artefakte legen.

## Eigener Kanal

Wer eigene Boxen mit eigener Software betreibt, setzt auf der Box `MYBOXI_AGENT_UPDATE_MANIFEST_URL` in `/etc/myboxi-agent/myboxi-agent.env` und legt seinen öffentlichen Schlüssel nach `/etc/myboxi-agent/update-keys/`. Der Server zeigt die neueste Version aus `MYBOXI_SERVER_UPDATE_MANIFEST_URL`.
