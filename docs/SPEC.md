# Box — Spezifikation v0.2: Datenmodell & Geräteprotokoll

Status: Entwurf · Stand: 2026-09-24 · Änderungen: §14
Scope: Der Vertrag zwischen **Box-Agent** (Raspberry Pi) und **Server**.
Nicht im Scope: Web-UI, Gehäuse, Image-Build, Rechtliches (eigene Dokumente).

Sprache der Spec: Deutsch. Code, Identifier, JSON-Felder, Topics: Englisch.

---

## 1. Leitprinzipien

1. **Offline-first.** Die Box ist die Wahrheit für den *Betrieb*, der Server die Wahrheit für die *Konfiguration*. Eine Box, die nie wieder Netz sieht, spielt alles weiter, was lokal gebunden und gecacht ist.
2. **Box ohne Server lauffähig.** Der Server ist optional. Meilenstein M0 funktioniert komplett ohne ihn.
3. **Nichts Sensibles verlässt die Box.** Kein Mikrofon-Audio, keine Spotify-Soloist-Keys, keine Nutzungsprotokolle über das in §9 Definierte hinaus.
4. **Spotify ist ein optionaler Provider**, kein Fundament. Kernprodukt = eigene Inhalte (Uploads, Aufnahmen, Podcasts).
5. **Self-hostable.** Die Server-URL ist konfigurierbar; eine Box kann ohne Neuinstallation auf eine andere Instanz umziehen.
6. **Versioniert.** Protokollversion steht im Topic-Pfad und in jeder Nachricht. Server unterstützt Version N und N-1.
7. **Kindgerecht robust.** Jede Fehlersituation endet in einem definierten, hörbaren Zustand (Ton oder Ansage), nie in Stille ohne Rückmeldung.

---

## 2. Begriffe

| Begriff | Englisch (Code) | Bedeutung |
|---|---|---|
| Mandant | `tenant` | Ein Haushalt. Abrechnungs- und Datengrenze. |
| Nutzer | `user` | Person mit Login (Eltern, Großeltern). Kann mehreren Mandanten angehören. |
| Mitgliedschaft | `membership` | Nutzer ↔ Mandant mit Rolle. |
| Box | `device` | Ein physisches Gerät. Gehört genau einem Mandanten. |
| Figur | `token` | Physischer NFC-Tag (NTAG213/215), identifiziert über UID. |
| Inhalt | `content` | Etwas Abspielbares: Upload-Sammlung, Podcast, Spotify-URI, Stream. |
| Datei | `asset` | Eine Audiodatei, content-adressiert über SHA-256. |
| Zuordnung | `binding` | Figur → Inhalt, **pro Mandant** (nicht pro Box). |

**Warum Bindings pro Mandant:** Wie bei der Toniebox funktioniert eine Figur auf jeder Box des Haushalts. Eine Figur, die der Box unbekannt ist (z. B. vom Besuchskind), löst ein `token_unknown`-Event aus und kann in der App zugeordnet werden.

---

## 3. Server-Datenmodell

Alle IDs sind UUIDv7 (zeitlich sortierbar), außer wo angegeben. Alle Tabellen haben `created_at`, `updated_at`. Mandanten-Isolation: jede mandantenbezogene Tabelle trägt `tenant_id`; Queries ohne `tenant_id`-Filter sind ein Bug.

### 3.1 `tenant`
| Feld | Typ | Notiz |
|---|---|---|
| id | uuid | |
| name | text | "Familie Muster" |
| plan | enum | `self_hosted`, `hosted_free`, `hosted_paid` |
| config_rev | bigint | Monoton steigend; +1 bei jeder Änderung an tokens/contents/bindings |

### 3.2 `user`, `membership`
`user`: id, email (unique), display_name, locale.

`membership`: tenant_id, user_id, role.

| Rolle | Darf |
|---|---|
| `owner` | alles inkl. Abrechnung, Mandant löschen, Nutzer einladen |
| `admin` | Boxen koppeln/entfernen, Regeln (Lautstärke, Ruhezeiten), alles von `contributor` |
| `contributor` | Inhalte hochladen, Figuren anlegen und zuordnen (Großeltern) |
| `viewer` | nur lesen |

### 3.3 `device`
| Feld | Typ | Notiz |
|---|---|---|
| id | uuid | **Auf der Box generiert** beim Erststart, persistent |
| tenant_id | uuid | null bis Pairing abgeschlossen |
| name | text | "Kinderzimmer" |
| hw_model | text | z. B. `rpi-zero2w`, `rpi4` |
| secret_hash | text | Argon2id-Hash des Device-Secrets |
| device_rev | bigint | Monoton; +1 bei Änderung an `device_config` |
| reported | jsonb | Letzter `reported`-Zustand (§6.4), vom Server nur gespeichert |
| mqtt_provisioned | bool | true, sobald Dynsec-Client und Rolle angelegt sind (§6) |
| last_seen_at | timestamptz | |

### 3.4 `device_config` (Soll-Zustand pro Box)
| Feld | Typ | Default | Notiz |
|---|---|---|---|
| max_volume | int 0–100 | 55 | Harte Obergrenze, auch für Tasten |
| start_volume | int 0–100 | 35 | Lautstärke nach Figur-Auflegen |
| quiet_hours | json | null | `{ "start": "19:30", "end": "06:30", "max_volume": 25 }` oder `"lock": true` |
| sleep_timer_min | int | null | Automatisch pausieren nach N Minuten |
| on_token_removed | enum | `pause` | `pause` \| `continue` |
| locale | text | `de-AT` | Für TTS-Ansagen |
| providers_enabled | text[] | `["local","podcast"]` | `spotify` nur wenn auf der Box eingerichtet |

### 3.5 `token` (Figur)
| Feld | Typ | Notiz |
|---|---|---|
| id | uuid | |
| tenant_id | uuid | |
| uid | text | NFC-UID, Hex, Großbuchstaben, ohne Trenner, z. B. `04A2B3C4D5E680` |
| label | text | "Bibi" |
| icon | text | optional, Emoji oder Asset-Referenz |

Unique: `(tenant_id, uid)`. Dieselbe physische Figur darf in mehreren Mandanten existieren.
Hinweis: UIDs sind nicht geheim und klonbar. Sie haben **keine** Sicherheitsfunktion.

### 3.6 `content`
| Feld | Typ | Notiz |
|---|---|---|
| id | uuid | |
| tenant_id | uuid | |
| kind | enum | `collection` \| `podcast` \| `spotify` \| `stream` |
| title | text | |
| cover_asset_id | uuid | optional |
| source | jsonb | kind-spezifisch, siehe unten |
| rev | int | +1 bei jeder Änderung |

`source` je `kind`:
- `collection`: `{}` — Einträge in `content_item`
- `podcast`: `{ "feed_url": "...", "keep_latest": 5, "order": "newest_first" }`
- `spotify`: `{ "uri": "spotify:album:..." }` — nur die URI, kein Web-API-Zugriff nötig
- `stream`: `{ "url": "https://..." }` — nur online spielbar

### 3.7 `content_item` (Titel einer `collection`)
content_id, position (int), asset_id, title, duration_ms.

### 3.8 `asset`
| Feld | Typ | Notiz |
|---|---|---|
| id | uuid | |
| tenant_id | uuid | |
| sha256 | text | Hex. Primärer Identifier für Box und Cache |
| mime | text | Auslieferungsformat: `audio/ogg; codecs=opus` |
| bytes | bigint | |
| storage_path | text | Relativer Pfad im Asset-Store, content-adressiert: `ab/cd/<sha256>.opus` |

Uploads werden serverseitig auf Opus (Mono, 48 kbit/s für Sprache, 96 kbit/s Stereo für Musik) transkodiert und loudness-normalisiert (EBU R128, −16 LUFS). Die Box bekommt nur transkodierte Assets.

Speicherung im Dateisystem unter einem konfigurierbaren Wurzelverzeichnis. Auslieferung: Die App prüft Berechtigung, nginx liefert die Datei per `X-Accel-Redirect` aus (Range-Requests und Caching ohne App-Last). Ein S3-Backend ist später hinter derselben Storage-Schnittstelle möglich.

### 3.9 `binding`
| Feld | Typ | Notiz |
|---|---|---|
| tenant_id | uuid | |
| token_id | uuid | Unique pro Mandant: eine Figur → genau ein Inhalt |
| content_id | uuid | |
| resume | bool | Default `true`: an letzter Position weiterspielen |
| shuffle | bool | Default `false` |
| repeat | enum | `off` \| `all` \| `one` |

### 3.10 `resume_position`
tenant_id, token_id, item_index, position_ms, updated_at (von der Box gemeldet), device_id.
Zweck: Backup und Box-übergreifendes Weiterhören. Konfliktregel §5.5.

### 3.11 `event`
id (ULID von der Box), tenant_id, device_id, type, data (jsonb), device_ts, received_at.
Aufbewahrung: 30 Tage, danach löschen. Nur die Typen aus §6.5.

---

## 4. Lokales Modell (Box)

SQLite unter `/var/lib/box/box.db` (auf der beschreibbaren Datenpartition, siehe Image-Spec).

Tabellen spiegeln den für die Box relevanten Ausschnitt: `token`, `content`, `content_item`, `binding`, `device_config`, `resume_position`. Zusätzlich:

| Tabelle | Zweck |
|---|---|
| `local_asset` | sha256, path, bytes, verified_at, last_played_at |
| `sync_state` | applied_config_rev, applied_device_rev, server_url |
| `staged_change` | Empfangene, noch nicht aktivierte Änderungen (wartet auf Assets) |
| `outbox` | Ausstehende Events, bis vom Server bestätigt |
| `secret` | device_secret; Soloist-API-Key (**nie** synchronisiert, nie geloggt) |

### 4.1 Cache-Regeln
- Alle Assets, die von einem aktiven Binding erreichbar sind, werden **vollständig vorab** geladen. Die Box spielt gebundene lokale Inhalte nie per Streaming.
- Podcasts: die Box lädt die neuesten `keep_latest` Episoden direkt aus dem Feed (Enclosure-URL), nicht über den Server.
- Spotify und Streams: kein Cache. Offline → Ansage "Das geht gerade leider nicht" + Fehlerton.
- Speicher knapp: nicht mehr gebundene Assets nach `last_played_at` (LRU) löschen. Gebundene Assets werden nie verdrängt; reicht der Platz nicht, meldet die Box `storage_full` (§6.5).
- Jedes Asset wird nach dem Download gegen `sha256` geprüft. Fehlschlag → verwerfen, erneut laden mit Backoff.

---

## 5. Sync-Modell

### 5.1 Grundidee
Revisionsbasiert. Der Server führt zwei Zähler: `tenant.config_rev` (Figuren, Inhalte, Zuordnungen) und `device.device_rev` (Box-Konfiguration). Die Box merkt sich, welche Revision sie zuletzt angewendet hat, und fragt nach dem Delta.

**MQTT transportiert nur Benachrichtigungen und kleine Nachrichten. Zustand und Dateien kommen über HTTPS.** Das hält MQTT-Payloads klein und erlaubt HTTP-Caching und Range-Requests für Audio.

### 5.2 Ablauf
1. Server ändert etwas → erhöht die Revision → publiziert `notify` (§6.1).
2. Box ruft `GET /api/v1/device/state?config_rev=X&device_rev=Y` auf.
3. Server antwortet mit Delta (wenn X noch im Änderungslog ist) oder vollem Snapshot (`"full": true`).
4. Box lädt alle neu benötigten Assets.
5. Box aktiviert die Änderung **atomar in einer SQLite-Transaktion** und setzt `applied_*_rev`.
6. Box meldet den neuen Stand in `reported`.

Zusätzlich zur Benachrichtigung pollt die Box alle 15 Minuten und direkt nach jedem Verbindungsaufbau. Verlorene `notify`-Nachrichten sind damit unkritisch.

### 5.3 Gestaffeltes Aktivieren
Eine Änderung, deren Assets noch nicht vollständig vorliegen, landet in `staged_change`.
- Das **alte** Binding bleibt aktiv, bis das neue komplett ist.
- Wird eine Figur aufgelegt, deren neues Binding noch lädt und die vorher kein Binding hatte: Ansage "Wird noch geladen" + Ladeton.
- Mehrere gestaffelte Änderungen werden in Revisionsreihenfolge aktiviert, nie übersprungen.

### 5.4 Delta-Format
```json
{
  "v": 1,
  "full": false,
  "config_rev": 142,
  "device_rev": 7,
  "upserts": {
    "token":        [ { "id": "...", "uid": "04A2B3C4D5E680", "label": "Bibi" } ],
    "content":      [ { "id": "...", "kind": "collection", "title": "...", "rev": 3 } ],
    "content_item": [ { "content_id": "...", "position": 0, "asset_sha256": "...", "title": "...", "duration_ms": 612000 } ],
    "binding":      [ { "token_id": "...", "content_id": "...", "resume": true, "shuffle": false, "repeat": "off" } ]
  },
  "deletes": {
    "token": ["..."], "content": ["..."], "binding": ["token_id..."]
  },
  "device_config": { "max_volume": 55, "start_volume": 35, "quiet_hours": null }
}
```
Bei `full: true` ersetzt die Box ihren gesamten Mandanten-Ausschnitt; `deletes` fehlt dann.
`content_item` wird pro `content` immer vollständig ersetzt, nicht einzeln gepatcht.

### 5.5 Konfliktregeln
Die Box hat keine eigene Konfigurations-UI, dadurch gibt es kaum echte Konflikte:

| Daten | Autorität | Regel |
|---|---|---|
| Figuren, Inhalte, Bindings, device_config | Server | Server gewinnt immer |
| Aktuelle Lautstärke | Box | Wird nur gemeldet, nie überschrieben (außer per `cmd`) |
| Resume-Position | Box | Last-Writer-Wins nach `updated_at`; bei mehreren Boxen gewinnt die jüngste Meldung |
| Soloist-Key, Device-Secret | Box | Werden nie übertragen |

### 5.6 Zeit ohne RTC
Pi Zero 2 W und Pi 4 haben keine Echtzeituhr. Nach einem Offline-Boot ist die Uhrzeit falsch.
- Die Box führt ein Flag `time_trusted` (true nach erfolgreichem NTP-Sync seit dem Boot).
- **Ruhezeiten** gelten nur bei `time_trusted`. Sonst gilt als sichere Rückfallebene `min(max_volume, quiet_hours.max_volume)` für die gesamte Laufzeit.
- Events tragen immer `boot_id` + monotone Millisekunden seit Boot zusätzlich zu `device_ts`. Der Server korrigiert Zeitstempel nachträglich, sobald die Box eine vertrauenswürdige Zeit meldet.
- Optional: DS3231-RTC-Modul als Hardware-Upgrade.

---

## 6. MQTT-Protokoll

**MQTT ist optional.** Ohne Broker funktioniert alles über Polling (§5.2), nur ohne sofortige Benachrichtigung und ohne Fernkommandos. Wichtig für einfache Self-Hosting-Setups.

Broker: Mosquitto 2 mit Dynamic-Security-Plugin, TLS direkt auf Port 8883 (Zertifikat wie nginx via certbot, Deploy-Hook). Kein Klartext-Port.
Topic-Präfix: `box/v1/{device_id}/`
ACL: Eine Box darf ausschließlich unter ihrem eigenen Präfix lesen und schreiben. Der Server legt beim Pairing pro Box einen Dynsec-Client (Username = `device_id`) und eine Rolle mit genau diesem Präfix an und entfernt beide beim Unpair.

### 6.0 Envelope (alle Nachrichten)
```json
{ "v": 1, "id": "01J8Z...ULID", "ts": "2026-09-24T18:02:11Z", "type": "…", "data": { } }
```

### 6.1 `notify` — Server → Box, QoS 1
```json
{ "type": "config_changed", "data": { "config_rev": 143, "device_rev": 7 } }
```

### 6.2 `cmd` — Server → Box, QoS 1
Jedes Kommando hat ein Ablaufdatum. **Abgelaufene Kommandos werden verworfen**, damit nicht Stunden später plötzlich Musik losgeht.
```json
{ "type": "cmd", "data": { "name": "stop", "args": {}, "expires_at": "2026-09-24T18:03:11Z" } }
```

| name | args | Zweck |
|---|---|---|
| `stop` | – | Wiedergabe stoppen |
| `set_volume` | `volume` | Wird auf `max_volume` begrenzt |
| `play_token` | `token_id` | "Schlaflied von der App aus starten" |
| `identify` | – | Box spielt Erkennungston (Welche Box ist das?) |
| `sync_now` | – | Sofortiger State-Abruf |
| `update_check` | – | Agent- und Soloist-Update prüfen |

Standard-TTL: 60 Sekunden.

### 6.3 `cmd/ack` — Box → Server, QoS 1
```json
{ "type": "cmd_ack", "data": { "cmd_id": "01J8Z...", "result": "ok" } }
```
`result`: `ok` \| `expired` \| `rejected` \| `error`, optional `message`.

### 6.4 `reported` — Box → Server, QoS 1, retained
Bei Änderung, höchstens alle 30 s, mindestens alle 10 min.
```json
{
  "type": "reported",
  "data": {
    "agent_version": "0.3.1",
    "image_version": "2026.09.1",
    "hw_model": "rpi-zero2w",
    "applied_config_rev": 143,
    "applied_device_rev": 7,
    "battery": { "percent": 72, "charging": false },
    "storage": { "free_mb": 9120 },
    "wifi_rssi": -61,
    "time_trusted": true,
    "playback": { "status": "playing", "token_id": "...", "volume": 35 },
    "soloist": { "installed": true, "build_expires_at": "2026-12-01" }
  }
}
```
Der Server warnt die Eltern, wenn `soloist.build_expires_at` weniger als 14 Tage entfernt ist und die Box das Update nicht selbst geschafft hat.

### 6.5 `events` — Box → Server, QoS 1
Die Box schreibt Events zuerst in die `outbox` und löscht sie erst nach PUBACK. Der Server dedupliziert über die Event-ID.

| type | data |
|---|---|
| `token_unknown` | `uid` |
| `token_played` | `token_id`, `content_id` |
| `playback_error` | `token_id`, `provider`, `code` |
| `storage_full` | `needed_mb`, `free_mb` |
| `sync_error` | `stage`, `code` |
| `resume_position` | `token_id`, `item_index`, `position_ms` |

Mehr wird nicht gemeldet. Kein Protokoll über Hördauer oder Tageszeiten jenseits dieser Events.

### 6.6 `online` — Last Will, retained
Box setzt beim Verbinden `"1"`, Broker setzt bei Verbindungsabbruch `"0"`.

---

## 7. HTTPS-API (geräteseitig)

Basis: `{server_url}/api/v1`. Alle Antworten JSON, außer Assets.

### 7.1 Pairing
Die Box hat kein Display, der Kopplungscode wird **per Sprachausgabe** angesagt.

1. Box: `POST /pairing/start`
   Body: `{ "device_id": "...", "hw_model": "rpi-zero2w", "agent_version": "0.3.1" }`
   Antwort: `{ "code": "471193", "expires_in": 600, "poll_token": "..." }`
   → Box sagt an: "Dein Code ist: vier – sieben – eins – eins – neun – drei" und wiederholt bei Tastendruck.
2. Nutzer (Rolle ≥ `admin`) in der App: `POST /tenants/{tid}/devices/claim` mit `{ "code": "471193", "name": "Kinderzimmer" }`
3. Box pollt: `GET /pairing/poll?poll_token=…` (alle 3 s)
   Nach Claim: `{ "device_secret": "...", "tenant_id": "...", "mqtt": { "host": "...", "port": 8883, "username": "<device_id>", "password": "..." } }`
   Secret und MQTT-Passwort werden **genau einmal** ausgeliefert. `mqtt` fehlt, wenn der Server ohne Broker betrieben wird.

Schutz: Codes 6-stellig, 10 min gültig, einmal verwendbar. Claim-Versuche pro Nutzer rate-limitiert (5/min, 20/h).

### 7.2 Authentifizierung
`POST /device/token` mit `{ "device_id", "device_secret" }` → kurzlebiges JWT (1 h).
Das JWT gilt nur für HTTPS. MQTT nutzt die beim Pairing angelegten eigenen Zugangsdaten (§6), damit der Broker ohne Auth-Plugin auskommt.

### 7.3 Endpunkte
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/device/state?config_rev=&device_rev=` | Delta oder Snapshot (§5.4) |
| GET | `/device/assets/{sha256}` | Audiodatei; `ETag` = sha256, Range-Requests Pflicht |
| POST | `/device/events` | Fallback, falls MQTT dauerhaft nicht erreichbar; Batch bis 100 Events |
| POST | `/device/unpair` | Box verlässt den Mandanten, lokaler Mandanten-Ausschnitt wird gelöscht |

---

## 8. Provider (Inhaltsquellen auf der Box)

Einheitliche Schnittstelle im Agent:
```
resolve(content) -> PlaybackPlan | Unavailable(reason)
```

| Provider | Offline | Umsetzung |
|---|---|---|
| `local` | ja | Assets aus Cache, lokaler Player (mpv oder GStreamer) |
| `podcast` | ja (gecachte Episoden) | Box lädt Episoden selbst aus dem Feed |
| `spotify` | nein | Soloist-WebSocket auf `127.0.0.1`, `play` mit `uri` |
| `stream` | nein | Direkt-URL |

### 8.1 Spotify-Provider
- Soloist wird **nicht** im Image ausgeliefert. Der Agent lädt es von der offiziellen Spotify-Downloadquelle nach, wenn der Nutzer Spotify auf der Box aktiviert.
- Der Soloist-API-Key wird ausschließlich über die lokale Setup-Seite der Box eingegeben (§9) und liegt nur in `secret`.
- Update-Job (systemd-Timer, täglich): neuen Build prüfen und installieren, sobald weniger als 30 Tage Restlaufzeit.
- **Wächter gegen Katalog-Drift:** Meldet Soloist ein `context_changed` auf einen Kontext, der nicht der gebundenen URI entspricht (z. B. durch Autoplay), pausiert der Agent sofort.

---

## 9. Box-Verhalten

### 9.1 Figuren-Logik
| Ereignis | Verhalten |
|---|---|
| Bekannte Figur aufgelegt | Start-Ton → Inhalt ab Resume-Position, Lautstärke `start_volume` |
| Unbekannte Figur | Freundlicher "Kenn ich nicht"-Ton, Event `token_unknown` |
| Figur entfernt | Gemäß `on_token_removed`, Position sichern |
| Inhalt zu Ende | Stille, Position auf Anfang; bei `repeat` entsprechend weiter |
| Provider nicht verfügbar | Ansage + Fehlerton, Event `playback_error` |

### 9.2 Lautstärke
`effektiv = min(gewünscht, max_volume, Ruhezeit-Grenze falls aktiv)`. Gilt für Tasten, `cmd` und Soloist gleichermaßen. Der Agent setzt die Soloist-Lautstärke aktiv zurück, falls sie von außen (Spotify-App) über die Grenze gestellt wird.

### 9.3 Lokale Setup-Seite
Nur im Setup-Modus erreichbar (Tastenkombination 5 s halten oder beim Erststart ohne WLAN). Die Box öffnet dann einen Access-Point mit Captive Portal für WLAN, Server-URL und optional Spotify-Key. Nach 15 min Inaktivität oder Abschluss wird der Modus beendet. Im Normalbetrieb lauscht die Box auf keinem Port außer lokal (`127.0.0.1`).

---

## 10. Sicherheit & Datenschutz

- TLS für HTTPS und MQTT, keine Ausnahmen.
- Device-Secrets serverseitig nur als Argon2id-Hash.
- Soloist-WebSocket ausschließlich auf `127.0.0.1` gebunden.
- Soloist-Key und Device-Secret nie in Logs, Crash-Reports oder Sync-Payloads.
- v1 ohne Mikrofon. Kommt Sprache in v2, bleibt die Verarbeitung vollständig lokal; kein Audio zum Server.
- Events: nur die Liste in §6.5, 30 Tage Aufbewahrung.
- Mandanten-Isolation serverseitig durchgängig; automatisierte Tests, die mandantenübergreifenden Zugriff versuchen, sind Teil der CI.

---

## 11. Versionierung & Updates

- Protokollversion im Topic (`box/v1/…`), im API-Pfad (`/api/v1`) und im Envelope (`"v": 1`).
- Breaking Changes nur mit neuer Hauptversion; der Server bedient N und N-1 parallel.
- Agent-Updates über eigenes apt-Repository, Kanäle `stable` und `beta`.
- Image-Updates sind nicht Teil dieser Spec.

---

## 12. Meilensteine

| # | Ziel | Server nötig |
|---|---|---|
| M0 | Agent offline: RFID, Tasten, lokale Assets aus einem Ordner, SQLite, Lautstärkeregeln, Resume | nein |
| M1 | Server-MVP: Mandanten, Nutzer, Rollen, Upload + Transkodierung, Figuren, Bindings; Pairing; `GET /device/state`; Asset-Download | ja |
| M2 | MQTT: `notify`, `cmd` mit TTL, `reported`, `events`, Outbox | ja |
| M3 | Podcast-Provider | ja |
| M4 | Spotify-Provider inkl. Update-Job und Katalog-Wächter | nein |
| M5 | Image-Build in CI (read-only Root, Setup-Modus) | – |

Der Agent wird in M0 gegen einen **Mock-Server** entwickelt, der die Endpunkte aus §7 mit statischen Fixtures bedient. Damit können Agent und Server ab M1 parallel entstehen.

---

## 13. Offene Punkte

- [ ] Läuft Pi Zero 2 W (512 MB) stabil mit PipeWire + Soloist + Agent? Vor Festlegung der Referenzhardware messen.
- [ ] Lassen sich Autoplay/Smart Shuffle in Soloist per CLI oder WebSocket abschalten, oder reicht nur der Wächter aus §8.1?
- [ ] Resume bei Spotify-Inhalten: `play` mit URI, danach `seek` — zuverlässig?
- [ ] TTS offline für Ansagen: Piper mit deutscher Stimme, Speicherbedarf auf Zero 2 W prüfen.
- [x] Tech-Stack festgelegt (siehe `CLAUDE.md`).
- [ ] Lizenz: AGPL-3.0 für Server; Agent AGPL-3.0 oder GPL-3.0.
- [ ] Rechtliche Einordnung Gehäuseverkauf (Produktsicherheit, Spielzeugrecht) — außerhalb dieser Spec.

---

## 14. Änderungen

**v0.2 (2026-09-24)**
- §3.3: `mqtt_provisioned` ergänzt.
- §3.8: Asset-Speicherung im Dateisystem, Auslieferung per nginx `X-Accel-Redirect` statt S3.
- §6: MQTT optional; Mosquitto mit Dynamic-Security-Plugin; Provisionierung pro Box beim Pairing.
- §7.1: Pairing-Antwort liefert MQTT-Zugangsdaten, Feld `mqtt` optional.
- §7.2: JWT nur noch für HTTPS, nicht mehr als MQTT-Passwort.
