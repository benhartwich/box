# Myboxi — Spezifikation v0.7: Datenmodell & Geräteprotokoll

Status: Entwurf · Stand: 2026-09-25 · Änderungen: §14
Scope: Der Vertrag zwischen **Box-Agent** (Raspberry Pi) und **Server**.
Nicht im Scope: Web-UI, Gehäuse, Image-Build, Rechtliches (eigene Dokumente).

Sprache der Spec: Deutsch. Code, Identifier, JSON-Felder, Topics: Englisch.
Projektname: **Myboxi** (technisch `myboxi`). „Box“ bezeichnet in dieser Spec das Gerät.

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

Neue Mandanten: Jeder angemeldete Nutzer kann einen Mandanten („Haushalt“) anlegen und wird dessen `owner`; höchstens 10 Mandanten mit Rolle `owner` je Nutzer. Den ersten Nutzer einer Installation legt der Betreiber an (`myboxi-server create-admin`).

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
| quiet_hours | json | null | `{ "start": "19:30", "end": "06:30", "max_volume": 25 }` oder `{ "start": "19:30", "end": "06:30", "lock": true }` — genau eines von `max_volume` und `lock`; `lock` = keine Wiedergabe |
| sleep_timer_min | int | null | Automatisch pausieren nach N Minuten |
| on_token_removed | enum | `pause` | `pause` \| `continue` |
| locale | text | `de-AT` | Für TTS-Ansagen |
| timezone | text | `Europe/Vienna` | IANA-Zeitzone, in der `quiet_hours` gelten |
| providers_enabled | text[] | `["local","podcast"]` | `spotify` nur wenn auf der Box eingerichtet |
| auto_update | bool | `true` | Software-Updates selbst installieren (§11); `false`: nur herunterladen |

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
| mime | text | Auslieferungsformat: `audio/ogg; codecs=opus`; Cover-Bilder: `image/jpeg` |
| bytes | bigint | |
| storage_path | text | Relativer Pfad im Asset-Store, content-adressiert: `ab/cd/<sha256>.opus` (Cover: `ab/cd/<sha256>.jpg`) |
| duration_ms | int | Nur Audio |

Cover (`content.cover_asset_id`) sind Bild-Assets (JPEG, 512×512) und nur für die App bestimmt; sie erscheinen nicht im State (§5.4), weil die Box kein Display hat.

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

SQLite unter `/var/lib/myboxi/myboxi.db` (auf der beschreibbaren Datenpartition, siehe Image-Spec).

Tabellen spiegeln den für die Box relevanten Ausschnitt: `token`, `content`, `content_item`, `binding`, `device_config`, `resume_position`. Zusätzlich:

| Tabelle | Zweck |
|---|---|
| `local_asset` | sha256, path, bytes, verified_at, last_played_at |
| `sync_state` | applied_config_rev, applied_device_rev, server_url |
| `staged_change` | Empfangene, noch nicht aktivierte Änderungen (wartet auf Assets) |
| `outbox` | Ausstehende Events, bis vom Server bestätigt |
| `secret` | device_secret; Soloist-API-Key (**nie** synchronisiert, nie geloggt) |

Pfade auf der Box: Daten unter `/var/lib/myboxi` (Datenbank `myboxi.db`, Assets content-adressiert unter `assets/ab/cd/<sha256>.opus`, eigene Ansagen unter `prompts/`), Konfiguration unter `/etc/myboxi-agent/myboxi-agent.env`.

**Lokale Bibliothek (M0, ohne Server):** Inhalte lassen sich direkt auf der Box anlegen (`myboxi-agent library add <UID> <Ordner>`). Sie tragen `origin = local` und gelten nur für Figuren, für die der Server kein Binding liefert. Ein Snapshot vom Server (§5.4) ersetzt ausschließlich Einträge mit `origin = server`.

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
    "content":      [ { "id": "...", "kind": "collection", "title": "...", "rev": 3, "source": {} } ],
    "content_item": [ { "content_id": "...", "position": 0, "asset_sha256": "...", "bytes": 3702144, "title": "...", "duration_ms": 612000 } ],
    "binding":      [ { "token_id": "...", "content_id": "...", "resume": true, "shuffle": false, "repeat": "off" } ]
  },
  "deletes": {
    "token": ["..."], "content": ["..."], "binding": ["token_id..."]
  },
  "device_config": {
    "max_volume": 55, "start_volume": 35, "quiet_hours": null, "sleep_timer_min": null,
    "on_token_removed": "pause", "locale": "de-AT", "timezone": "Europe/Vienna",
    "providers_enabled": ["local", "podcast"]
  }
}
```
Bei `full: true` ersetzt die Box ihren gesamten Mandanten-Ausschnitt; `deletes` fehlt dann.
- Ein Snapshot enthält **alle** Figuren, Inhalte, Content-Items und Bindings des Mandanten. Welche Assets sie lädt, entscheidet die Box nach §4.1.
- `content.source` wird immer mitgeliefert (§3.6); die Box braucht Feed-URL, Spotify-URI bzw. Stream-URL.
- `content_item.bytes` ist die Größe des Assets, damit die Box Speicher planen und `storage_full.needed_mb` berechnen kann.
- `device_config` wird immer vollständig übertragen (alle Felder aus §3.4).
- `token.icon` und Cover werden nicht übertragen. Resume-Positionen sind (noch) nicht Teil des States.
- Ein Server darf immer mit `full: true` antworten. Der Server-MVP (M1) liefert ausschließlich Snapshots.
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
- **Ruhezeiten** gelten nur bei `time_trusted`. Sonst gilt als sichere Rückfallebene `min(max_volume, quiet_hours.max_volume)` für die gesamte Laufzeit. Ruhezeiten mit `lock` lassen sich ohne verlässliche Zeit nicht zuordnen; dann gilt keine Sperre, sondern nur `max_volume` (sonst bliebe die Box nach jedem Offline-Start stumm).
- Events tragen immer `boot_id` + monotone Millisekunden seit Boot (`mono_ms`, Envelope-Felder, §6.0) zusätzlich zu `device_ts` (= Envelope-`ts`). Der Server speichert beide und korrigiert Zeitstempel nachträglich, sobald die Box eine vertrauenswürdige Zeit meldet (Korrektur folgt nach M1).
- Optional: DS3231-RTC-Modul als Hardware-Upgrade.

---

## 6. MQTT-Protokoll

**MQTT ist optional.** Ohne Broker funktioniert alles über Polling (§5.2), nur ohne sofortige Benachrichtigung und ohne Fernkommandos. Wichtig für einfache Self-Hosting-Setups.

Broker: Mosquitto 2 mit Dynamic-Security-Plugin, TLS direkt auf Port 8883 (Zertifikat wie nginx via certbot, Deploy-Hook). Kein Klartext-Port.
Topic-Präfix: `myboxi/v1/{device_id}/`
ACL: Eine Box darf ausschließlich unter ihrem eigenen Präfix lesen und schreiben. Der Server legt beim Pairing pro Box einen Dynsec-Client (Username = `device_id`) und eine Rolle mit genau diesem Präfix an und entfernt beide beim Unpair.

### 6.0 Envelope (alle Nachrichten)
```json
{ "v": 1, "id": "01J8Z...ULID", "ts": "2026-09-24T18:02:11Z", "type": "…", "data": { } }
```
Optional (Pflicht für Events nach §6.5, siehe §5.6): `"boot_id": "<uuid>"`, `"mono_ms": 123456` (Millisekunden seit Boot).
`id` ist eine ULID (26 Zeichen, Crockford-Base32), `ts` ein RFC-3339-Zeitstempel in UTC.

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
Bei Änderung, höchstens alle 30 s (in der Einrichtungsphase alle 5 s, §9.6), mindestens alle 10 min. Ohne MQTT-Verbindung sendet die Box denselben Envelope per `POST /device/reported` (§7.3).
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
    "soloist": { "installed": true, "build_expires_at": "2026-12-01" },
    "health": [
      { "check": "nfc", "level": "ok", "code": "ok" },
      { "check": "audio", "level": "fail", "code": "no_output" }
    ],
    "button_test": { "seen": ["play_pause", "volume_up"] },
    "update": { "state": "waiting", "version": "0.3.0" }
  }
}
```
Der Server warnt die Eltern, wenn `soloist.build_expires_at` weniger als 14 Tage entfernt ist und die Box das Update nicht selbst geschafft hat.

Optionale Felder (fehlen sie, weiß der Server es nicht):
- `wifi_rssi`: Signalstärke des WLANs in dBm.
- `health`: Selbsttest der Box, ein Eintrag je Prüfung. `level` ist `ok`, `warn` oder `fail`. `check` und `code` sind Maschinencodes (`^[a-z0-9_]{1,32}$`); unbekannte Werte zeigt der Server neutral an, damit neue Prüfungen ältere Server nicht brechen. Kein Freitext, keine WLAN-Namen, keine Pfade.

  | `check` | `code` bei Problemen |
  |---|---|
  | `nfc` | `no_i2c` (I2C-Bus fehlt), `not_responding` (Leser antwortet nicht), `read_error` (Lesefehler, Leser wird neu geöffnet) |
  | `audio` | `player_down` (Wiedergabeprozess läuft nicht), `no_output` (kein Audioausgang) |
  | `buttons` | `gpio_error` (Taster lassen sich nicht einrichten) |
  | `prompts` | `missing` (Ansagen fehlen) |
- `button_test`: nur in der Einrichtungsphase (§9.6); die Namen der Tasten (§9.4), die seit Beginn der Phase gedrückt wurden.
- `update`: Stand der Software-Updates (§11). `state` ist einer von `up_to_date`, `available` (neue Version bekannt, `auto_update` aus), `downloading`, `waiting` (geladen, wartet auf Ruhe), `installed` (seit dem letzten Update läuft die neue Version), `failed`, `rolled_back` (neue Version war nicht gesund, die alte läuft). `version`: die betroffene Version. `code` (nur bei `failed`/`rolled_back`) ist ein Maschinencode wie `bad_signature`, `checksum`, `no_space`, `download`, `unhealthy`.

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
Der Envelope-`type` ist der Event-Typ aus der Tabelle. `resume_position` aktualisiert serverseitig §3.10 nach Last-Writer-Wins über `ts`.

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

Antworten auf `/pairing/poll`:

| Lage | Status | Body |
|---|---|---|
| Noch nicht beansprucht | 202 | `{ "status": "pending", "expires_in": 412 }` |
| Beansprucht | 200 | wie oben (einmalig) |
| Abgelaufen / Secret bereits ausgeliefert / durch anderen Claim entwertet | 410 | Fehler `pairing_expired` bzw. `pairing_consumed` |
| Unbekannter `poll_token` | 404 | Fehler `not_found` |
| Schneller als alle 2 s gepollt | 429 | Fehler `rate_limited` |

Claim-Antwort: `200 { "device_id": "...", "name": "Kinderzimmer" }`. Der Claim-Endpunkt ist ein Nutzer-Endpunkt (Sitzungs-Cookie + CSRF-Header).

Schutz: Codes 6-stellig, 10 min gültig, einmal verwendbar. Claim-Versuche pro Nutzer rate-limitiert (5/min, 20/h), zusätzlich 30/h pro Mandant. `/pairing/start` höchstens 10/h pro IP.
Das Device-Secret entsteht erst beim ausliefernden Poll und wird nur als Argon2id-Hash gespeichert.

Erneutes Pairing:
- Eine bereits gekoppelte Box darf `/pairing/start` erneut aufrufen (z. B. wenn die Poll-Antwort verloren ging). Beanspruchen darf den Code dann nur derselbe Mandant, sonst `409 device_paired_elsewhere`. Ein Mandantenwechsel erfordert vorher `unpair` (durch die Box oder in der App).
- Pro Box dürfen mehrere Codes gleichzeitig offen sein; ein erfolgreicher Claim entwertet alle anderen offenen Codes dieser Box.
- Nach erneutem Pairing sind alle vorher ausgestellten Tokens der Box ungültig.

### 7.2 Authentifizierung
`POST /device/token` mit `{ "device_id", "device_secret" }` → `{ "access_token": "<JWT>", "token_type": "Bearer", "expires_in": 3600 }`.
Unbekannte Box, falsches Secret und entkoppelte Box ergeben einheitlich `401 invalid_credentials`. Höchstens 10 Versuche pro Minute und Box.
Alle weiteren Geräte-Endpunkte erwarten `Authorization: Bearer <JWT>`. Nach `unpair` oder erneutem Pairing werden bestehende Tokens sofort abgelehnt (`401 unauthorized`).
Das JWT gilt nur für HTTPS. MQTT nutzt die beim Pairing angelegten eigenen Zugangsdaten (§6), damit der Broker ohne Auth-Plugin auskommt.

### 7.3 Endpunkte
| Methode | Pfad | Zweck |
|---|---|---|
| GET | `/device/state?config_rev=&device_rev=` | Delta oder Snapshot (§5.4) |
| GET | `/device/assets/{sha256}` | Audiodatei; `ETag` = sha256, Range-Requests Pflicht |
| POST | `/device/events` | Fallback, falls MQTT dauerhaft nicht erreichbar; Batch bis 100 Events |
| POST | `/device/reported` | Fallback für `reported` (§6.4) ohne MQTT; Body = Envelope, Antwort `204` |
| POST | `/device/unpair` | Box verlässt den Mandanten, lokaler Mandanten-Ausschnitt wird gelöscht; Antwort `204` |

`/device/events`:
```json
{ "events": [ { "v": 1, "id": "01J8Z...", "ts": "...", "type": "token_unknown", "boot_id": "...", "mono_ms": 81234, "data": { "uid": "04A2B3C4D5E680" } } ] }
```
Antwort `200`:
```json
{ "results": [ { "id": "01J8Z...", "status": "accepted" } ] }
```
`status`: `accepted` \| `duplicate` \| `rejected` (dann mit `code`). Die Box entfernt **alle** in `results` genannten IDs aus der `outbox`; `rejected` ist endgültig (z. B. unbekannter Typ).

`/device/assets/{sha256}`: `ETag: "<sha256>"`, `If-None-Match` → `304`. Eine Box erhält jedes Asset ihres Mandanten, fremde Assets ergeben `404`.

### 7.4 Fehlerformat
Alle Fehlerantworten der Geräte-API:
```json
{ "error": { "code": "pairing_expired", "message": "Pairing code expired" } }
```
Codes: `invalid_request`, `unauthorized`, `invalid_credentials`, `not_found`, `rate_limited` (mit `Retry-After`), `pairing_expired`, `pairing_consumed`, `code_invalid`, `device_paired_elsewhere`.

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

Details:
- Der Setup-Modus startet automatisch, wenn kein WLAN konfiguriert ist oder das konfigurierte WLAN 2 min lang nicht erreichbar ist und keine Ethernet-Verbindung besteht; außerdem mit `volume_up` + `volume_down` 5 s gehalten (§9.4).
- Offenes WLAN `Myboxi-NNNN` (vier Ziffern aus der Seriennummer, damit die Box den Namen mit ihren Ziffern-Ansagen vorlesen kann), Seite unter `http://10.42.0.1/`; alle DNS-Anfragen zeigen dorthin (Captive Portal).
- Felder: WLAN (Liste oder manuell) und Server-URL (Vorgabe `https://app.myboxi.eu`). Das Feld für den Spotify-Key kommt mit M4.
- Die Box sagt Beginn und Ende des Setup-Modus an und ob die Verbindung geklappt hat.

### 9.4 Tasten
| Eingabe | Wirkung |
|---|---|
| `play_pause` kurz | Pause bzw. weiterspielen; während der Kopplung: Code wiederholen |
| `volume_up` / `volume_down` kurz | Lautstärke ±5 (über §9.2); gehalten alle 250 ms wiederholt |
| `next` kurz | Nächster Titel; am Ende gemäß `repeat` (`all`: erster Titel, sonst Stille und Position auf Anfang) |
| `volume_up` + `volume_down` 5 s | Setup-Modus (§9.3) |
| `play_pause` + `next` 5 s | Box entkoppeln (`/device/unpair`) und neu koppeln (§9.5) |

### 9.5 Kopplung auf der Box
- Eine ungekoppelte Box mit Server-URL startet die Kopplung (§7.1) automatisch, sobald sie online ist.
- Sie sagt den Code nach einem Hinweiston an, danach alle 30 s und bei `play_pause` erneut.
- Läuft der Code ab, holt sie einen neuen. Nach erfolgreicher Kopplung: Bestätigungsansage und sofortige Synchronisierung.

### 9.6 Einrichtungsphase
Die ersten 60 min nach einer erfolgreichen Kopplung, gemessen ab `paired_at` (Wanduhr der Box; ohne verlässliche Zeit nur bis zum nächsten Neustart). In dieser Phase:
- synchronisiert die Box alle 30 s statt im normalen Intervall;
- sendet sie `reported` bei Änderung höchstens alle 5 s statt alle 30 s (§6.4);
- sammelt sie die gedrückten Tasten für `button_test` (§6.4);
- quittiert sie jeden Tastendruck, während nichts spielt, mit einem kurzen Ton (prüft zugleich den Lautsprecher).

---

## 10. Sicherheit & Datenschutz

- TLS für HTTPS und MQTT, keine Ausnahmen.
- Device-Secrets serverseitig nur als Argon2id-Hash.
- Soloist-WebSocket ausschließlich auf `127.0.0.1` gebunden.
- Soloist-Key und Device-Secret nie in Logs, Crash-Reports oder Sync-Payloads.
- v1 ohne Mikrofon. Kommt Sprache in v2, bleibt die Verarbeitung vollständig lokal; kein Audio zum Server.
- Events: nur die Liste in §6.5, 30 Tage Aufbewahrung.
- `button_test` enthält nur Tastennamen, keine Zeitpunkte, und nur während der Einrichtungsphase (§9.6).
- Software-Updates (§11) installiert nur ein eigener Root-Dienst auf der Box, nie der Agent selbst. Die Box vertraut ausschließlich signierten Manifesten (Ed25519, Schlüssel im Image) und installiert nie eine ältere oder gleiche Version.
- Mandanten-Isolation serverseitig durchgängig; automatisierte Tests, die mandantenübergreifenden Zugriff versuchen, sind Teil der CI.

---

## 11. Versionierung & Updates

- Protokollversion im Topic (`myboxi/v1/…`), im API-Pfad (`/api/v1`) und im Envelope (`"v": 1`).
- Breaking Changes nur mit neuer Hauptversion; der Server bedient N und N-1 parallel.
- Image-Updates (ganzes Betriebssystem, A/B-Partitionen) sind nicht Teil dieser Spec.

### 11.1 Software-Updates der Box
Die Box aktualisiert den Agent (samt Ansagen) selbst, sobald sie online ist.

- **Paket**: `myboxi-agent-<version>-arm64.tar.xz` enthält das Verzeichnis `<version>/` (Venv und Ansagen) für `/opt/myboxi-agent/releases/`.
- **Manifest** je Kanal (vorerst nur `stable`), JSON, dazu `manifest.json.sig` (Ed25519 über die Bytes der Datei, roh, 64 Byte):
  ```json
  { "channel": "stable", "version": "0.3.0", "released_at": "2026-09-25T12:00:00Z",
    "bundle": { "url": "https://…/myboxi-agent-0.3.0-arm64.tar.xz", "sha256": "…", "size": 41234567 } }
  ```
  Standard-Ort: `https://github.com/benhartwich/myboxi/releases/download/channel-stable/manifest.json`, auf der Box einstellbar.
- **Vertrauen**: Ein Manifest gilt nur mit gültiger Signatur eines Schlüssels aus `/etc/myboxi-agent/update-keys/`. Die Box installiert nur Versionen, die höher sind als die laufende; das Paket muss die SHA-256 und Größe aus dem Manifest haben.
- **Ablauf**: prüfen nach jedem Verbindungsaufbau und stündlich → herunterladen (fortsetzbar) → prüfen → entpacken nach `releases/<version>` → warten, bis nichts spielt → Symlink `current` atomar umstellen → Agent neu starten → gesund, wenn der neue Agent sich innerhalb von 90 s mit der neuen Version meldet; sonst zurück auf die alte Version (`rolled_back`). Es bleiben die zwei neuesten Versionen.
- **Mit `auto_update = false`** lädt die Box, installiert aber nicht (`available`).
- **Betriebssystem**: Sicherheitsupdates von Debian und Raspberry Pi OS installiert `unattended-upgrades`. Braucht ein Update einen Neustart, startet die Box neu, wenn 10 min lang nichts gespielt hat.

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
- [x] Lizenz: Server AGPL-3.0-or-later, Agent GPL-3.0-or-later, `packages/protocol` Apache-2.0, Spezifikation und Doku CC BY 4.0 (siehe `REUSE.toml`).
- [ ] Rechtliche Einordnung Gehäuseverkauf (Produktsicherheit, Spielzeugrecht) — außerhalb dieser Spec.

---

## 14. Änderungen

**v0.7 (2026-09-25)** — Software-Updates der Box; Protokollversion bleibt `v1`, alle Änderungen additiv.
- §3.4: `auto_update`.
- §6.4: optionales Feld `update`.
- §10: Update-Dienst und Signaturen.
- §11: signierte Agent-Updates statt apt-Repository; Sicherheitsupdates des Betriebssystems.

**v0.6 (2026-09-25)** — Einrichtung mit Selbsttest; Protokollversion bleibt `v1`, alle Änderungen additiv.
- §3.2: Nutzer legen Mandanten selbst an (höchstens 10 als `owner`).
- §6.4: optionale Felder `health` und `button_test`; `wifi_rssi` erläutert.
- §9.6: Einrichtungsphase nach der Kopplung.
- §10: Datenschutz von `button_test`.

**v0.5 (2026-09-24)** — Box-Verhalten für den Agent; Protokollversion bleibt `v1`.
- §4: Pfade auf der Box; lokale Bibliothek mit `origin = local` (M0).
- §5.6: Ruhezeiten mit `lock` ohne verlässliche Zeit.
- §9.3: Setup-Modus konkretisiert (Auslöser, WLAN-Name, Portal-Adresse, Felder).
- §9.4: Tastenbelegung und Kombinationen.
- §9.5: Kopplungsablauf auf der Box.

**v0.4 (2026-09-24)** — Projektname Myboxi. Protokollversion bleibt `v1`.
- §6, §11: MQTT-Topic-Präfix `myboxi/v1/{device_id}/` statt `box/v1/…`. MQTT ist noch nicht implementiert (M2), daher ohne Migrationsbedarf.
- §4: SQLite der Box unter `/var/lib/myboxi/myboxi.db`.
- Technische Namen: Pakete `myboxi_protocol`, `myboxi_server`, `myboxi_agent`; JWT-Audience `myboxi-device`.
- §13: Lizenzen festgelegt.

**v0.3 (2026-09-24)** — Klarstellungen für den Server-MVP (M1); Protokollversion bleibt `v1`, alle Änderungen additiv.
- §3.4: `quiet_hours` genau spezifiziert (`max_volume` oder `lock`); neues Feld `timezone`.
- §3.8: Cover als Bild-Assets (`image/jpeg`, `.jpg`), nicht im State; `duration_ms`.
- §5.4: Snapshot-Umfang; `content.source` und `content_item.bytes` im State; `device_config` immer vollständig.
- §5.6, §6.0: Envelope-Felder `boot_id` und `mono_ms`.
- §6.4, §7.3: `POST /device/reported` als HTTP-Fallback.
- §7.1: Poll-Antworten, Claim-Antwort, erneutes Pairing, zusätzliche Rate-Limits.
- §7.2: Token-Antwort, einheitliches `invalid_credentials`, Token-Entwertung nach Unpair/Re-Pairing.
- §7.3: Format von `/device/events`; `/device/assets` mit `ETag`/`304`.
- §7.4: Fehlerformat.

**v0.2 (2026-09-24)**
- §3.3: `mqtt_provisioned` ergänzt.
- §3.8: Asset-Speicherung im Dateisystem, Auslieferung per nginx `X-Accel-Redirect` statt S3.
- §6: MQTT optional; Mosquitto mit Dynamic-Security-Plugin; Provisionierung pro Box beim Pairing.
- §7.1: Pairing-Antwort liefert MQTT-Zugangsdaten, Feld `mqtt` optional.
- §7.2: JWT nur noch für HTTPS, nicht mehr als MQTT-Passwort.
