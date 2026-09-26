# Myboxi selbst hosten

Myboxi ist komplett Open Source. Wer nicht `app.myboxi.eu` nutzen möchte, betreibt den Server selbst: zu Hause auf einem kleinen Rechner, einem NAS oder einem gemieteten Server. Die Boxen zeigen dann auf diesen Server; `app.myboxi.eu` ist nicht beteiligt. Figuren, die schon auf der Box sind, spielen ohnehin auch ganz ohne Server.

Es gibt zwei Wege:

| Weg | Für wen | Anleitung |
|---|---|---|
| **Docker Compose** | Schnell eingerichtet, auch auf NAS und Raspberry Pi | dieses Dokument |
| **Debian 13 direkt** | Wer lieber ohne Docker arbeitet; so läuft `app.myboxi.eu` | [betrieb-debian13.md](betrieb-debian13.md) |

## Was du brauchst

- Einen Rechner mit Linux und Docker samt Compose-Plugin (`docker compose version`). Ein Raspberry Pi 4 oder 5 mit 64-Bit-System reicht. 1 GB RAM genügt, 2 GB sind angenehmer. Dazu Platz für die Audiodateien.
- Einen Namen, unter dem Boxen und Handys den Server erreichen, und ein Zertifikat dafür (Abschnitt 1). Die Box spricht mit dem Server nur verschlüsselt, auch im Heimnetz. So kann niemand im WLAN ihre Zugangsdaten mitlesen.

## 1. Zertifikat wählen

| Lage | Weg |
|---|---|
| Der Server ist aus dem Internet erreichbar (Ports 80 und 443) | [1a: Let's Encrypt automatisch](#1a-lets-encrypt-automatisch) |
| Eigene Domain, aber nichts nach außen öffnen | [1b: Let's Encrypt über DNS](#1b-lets-encrypt-über-dns-keine-offenen-ports) |
| Nur im Heimnetz, ohne eigene Domain | [1c: Eigene Zertifizierungsstelle](#1c-eigene-zertifizierungsstelle-nur-heimnetz) |

Das Zertifikat liegt am Ende immer als `fullchain.pem` und `privkey.pem` in `deploy/docker/certs/`. nginx und Mosquitto merken innerhalb von fünf Minuten, wenn es erneuert wurde, und laden es neu; die Boxen bleiben verbunden.

### 1a: Let's Encrypt automatisch

Voraussetzungen:
- Ein DNS-Eintrag für deinen Namen zeigt auf den Server.
- Die Ports 80 und 443 sind aus dem Internet erreichbar, zu Hause per Portweiterleitung am Router.

In `.env` (Abschnitt 2) eintragen:

```text
COMPOSE_PROFILES=letsencrypt
MYBOXI_LETSENCRYPT_EMAIL=ich@example.org
```

Der Dienst `certbot` holt das Zertifikat beim ersten Start und prüft alle 12 Stunden, ob es erneuert werden muss. Bis das erste Zertifikat da ist, nutzt nginx kurz ein vorläufiges.

### 1b: Let's Encrypt über DNS (keine offenen Ports)

Let's Encrypt prüft hier über einen DNS-Eintrag, dass dir die Domain gehört. Dafür braucht es einen Zugang zur DNS-API deines Domain-Anbieters. Am Server muss kein Port von außen erreichbar sein. Der DNS-Eintrag für den Namen darf auf die Adresse im Heimnetz zeigen, z. B. `192.168.1.20`.

Beispiel mit Cloudflare. Für andere Anbieter gibt es passende Images (`certbot/dns-route53`, `certbot/dns-digitalocean`, `certbot/dns-ovh` und weitere) oder Werkzeuge wie `acme.sh`.

```text
cd deploy/docker
mkdir -p letsencrypt certs
printf 'dns_cloudflare_api_token = DEIN-TOKEN\n' > cloudflare.ini && chmod 600 cloudflare.ini
docker run --rm -v "$PWD/letsencrypt:/etc/letsencrypt" -v "$PWD/cloudflare.ini:/cloudflare.ini:ro" \
  certbot/dns-cloudflare certonly --dns-cloudflare --dns-cloudflare-credentials /cloudflare.ini \
  -d myboxi.example.org -m ich@example.org --agree-tos -n
cp -L letsencrypt/live/myboxi.example.org/fullchain.pem certs/fullchain.pem
install -m 600 "$(readlink -f letsencrypt/live/myboxi.example.org/privkey.pem)" certs/privkey.pem
```

Erneuern: denselben `docker run …` mit `renew` statt `certonly …` einmal im Monat ausführen, z. B. per Cronjob. Danach die beiden Dateien wieder nach `certs/` kopieren.

Tipp: Manche Router verwerfen DNS-Antworten mit Heimnetz-Adressen (Schutz vor „DNS-Rebinding“). Bei einer Fritz!Box den Namen unter *Heimnetz → Netzwerk → Netzwerkeinstellungen* beim „DNS-Rebind-Schutz“ als Ausnahme eintragen.

### 1c: Eigene Zertifizierungsstelle (nur Heimnetz)

Ohne Domain erstellst du dir eine eigene Zertifizierungsstelle (CA). Das Skript braucht nur `openssl`:

```text
cd deploy/docker
./own-ca.sh myboxi.home.arpa 192.168.1.20
```

**Erster Parameter:** der Name des Servers.
- `.home.arpa` ist für Heimnetze vorgesehen.
- Ein Name, den dein Router auflöst, geht auch, z. B. `myboxi.fritz.box` bei einer Fritz!Box.

Der Name muss später genau so in `MYBOXI_DOMAIN` und in der Server-Adresse der Box stehen.

**Zweiter Parameter (optional):** die feste IP-Adresse des Servers, zusätzlich im Zertifikat.

Löst dein Router gar keine Namen auf, nimmst du statt des Namens die IP-Adresse:
- `./own-ca.sh 192.168.1.20` und `MYBOXI_DOMAIN=192.168.1.20`.
- Die Box und der Browser nutzen dann `https://192.168.1.20`.
- Die Adresse gibst du dem Server am besten fest im Router.

Das Skript legt an:
- `certs/myboxi-ca.pem`: das CA-Zertifikat. Das bekommen die Box (Abschnitt 4) und, wenn du keine Warnung im Browser willst, eure Handys.
- `certs/ca/ca.key`: der Schlüssel der CA. **Er bleibt geheim** und verlässt den Server nie. Wer ihn hat, kann Zertifikate ausstellen, denen eure Geräte vertrauen. Die Box vertraut der CA nur für den Myboxi-Server, nie für andere Verbindungen.
- `certs/fullchain.pem`, `certs/privkey.pem`: das Zertifikat des Servers, 825 Tage gültig. Vorher das Skript einfach noch einmal aufrufen. Die CA bleibt dieselbe, die Boxen müssen nichts neu lernen.

**CA auf dem Handy installieren (optional):**
- Android: `myboxi-ca.pem` aufs Handy laden, dann in den Einstellungen unter *Sicherheit* (je nach Hersteller „Verschlüsselung und Anmeldedaten“) *Zertifikat installieren → CA-Zertifikat* wählen.
- iPhone: Datei öffnen, das Profil unter *Einstellungen → Allgemein → VPN und Geräteverwaltung* installieren. Danach unter *Einstellungen → Allgemein → Info → Zertifikatsvertrauenseinstellungen* „volles Vertrauen“ einschalten.

## 2. Einrichten

```text
git clone https://github.com/benhartwich/myboxi.git
cd myboxi/deploy/docker
cp env.example .env && chmod 600 .env
sed -i "s|^POSTGRES_PASSWORD=.*|POSTGRES_PASSWORD=$(openssl rand -hex 24)|" .env
sed -i "s|^MYBOXI_DEVICE_JWT_KEY=.*|MYBOXI_DEVICE_JWT_KEY=$(openssl rand -base64 48 | tr -d '\n')|" .env
```

In `.env` den Namen eintragen (`MYBOXI_DOMAIN=…`) und bei Bedarf die Profile aus Abschnitt 1a und 3. Dann das Zertifikat nach `certs/` legen (Abschnitt 1) und starten:

```text
docker compose up -d --build
docker compose run --rm api myboxi-server create-admin --email ich@example.org --tenant-name "Familie Muster"
```

Das Passwort wird abgefragt, mindestens 10 Zeichen. Danach `https://<dein Name>` im Browser öffnen und anmelden. Weitere Personen lädst du in der Web-UI ein. Ohne Mailserver zeigt die Web-UI den Einladungslink an; für den Versand per Mail die `MYBOXI_SMTP_*`-Zeilen in `.env` ausfüllen.

Die Ports gelten auf allen Adressen des Rechners. Nur im Heimnetz erreichbar: `MYBOXI_LISTEN=192.168.1.20` (die Heimnetz-Adresse des Servers).

Ist Port 443 schon belegt, z. B. auf einem NAS:
- `MYBOXI_HTTPS_PORT=8443` und `MYBOXI_BASE_URL=https://<dein Name>:8443` setzen.
- In der Box die Adresse mit `:8443` eintragen.

## 3. Sofort-Abgleich und Fernbedienung (optional)

Ohne MQTT fragen die Boxen alle 15 Minuten nach Änderungen. Mit MQTT kommen Änderungen sofort an, und die Box-Seite zeigt eine Fernbedienung (Stopp, Lautstärke, Figur abspielen, „Welche Box ist das?“).

```text
sed -i "s|^MYBOXI_MQTT_PASSWORD=.*|MYBOXI_MQTT_PASSWORD=$(openssl rand -hex 32)|" .env
# in .env: COMPOSE_PROFILES=mqtt   (mit Let's Encrypt: COMPOSE_PROFILES=mqtt,letsencrypt)
docker compose up -d
```

Der Broker lauscht nur verschlüsselt auf Port 8883. Jede Box bekommt beim Koppeln ein eigenes Konto, das nur ihre eigenen Nachrichten sieht. Port 8883 muss für die Boxen erreichbar sein; aus dem Internet nur, wenn Boxen außer Haus stehen. Boxen, die schon vorher gekoppelt waren, bekommen MQTT nach einmaligem Neu-Koppeln (`play_pause` + `next` 5 Sekunden halten).

## 4. Die Box mit deinem Server verbinden

Beim Einrichten der Box ([box-inbetriebnahme.md](box-inbetriebnahme.md)):

1. Im Setup-Portal unter **Server** deine Adresse eintragen, z. B. `https://myboxi.home.arpa`.
2. Nur mit eigener CA (1c): **Eigenes Zertifikat (optional)** aufklappen und den ganzen Inhalt von `certs/myboxi-ca.pem` einfügen, von `-----BEGIN CERTIFICATE-----` bis `-----END CERTIFICATE-----`.
3. Verbinden. Die Box sagt einen Code an; in deiner Web-UI unter **Boxen → Box hinzufügen** eintragen.

Wer per SSH auf der Box ist, geht auch so:

```text
myboxi-agent server https://myboxi.home.arpa --ca myboxi-ca.pem
```

Die Box nutzt das eigene Zertifikat nur für deinen Server. Podcasts, Spotify und Software-Updates laufen weiter über die normalen, öffentlich geprüften Zertifikate. Stellst du die Box auf einen anderen Server um, vergisst sie das Zertifikat.

## 5. Spotify-Suche

Die Suche nutzt die eigene Spotify-App eures Haushalts. Als Redirect URI trägst du im [Spotify-Entwicklerportal](https://developer.spotify.com/dashboard) die Adresse deines Servers ein: `https://<dein Name>/spotify/callback`. Alles Weitere steht in [box-inbetriebnahme.md](box-inbetriebnahme.md), Abschnitt „Spotify“.

## 6. Aktualisieren

```text
cd myboxi && git pull
cd deploy/docker && docker compose up -d --build
```

Datenbank-Migrationen laufen beim Start von selbst (Dienst `migrate`). Die Software der Boxen kommt unabhängig davon als signiertes Update aus den GitHub-Releases ([updates.md](updates.md)). Wer auch das selbst in der Hand haben will, betreibt einen eigenen Update-Kanal (dort unter „Eigener Kanal“).

## 7. Sichern und wiederherstellen

Zu sichern sind die Datenbank, die Audiodateien, `.env` und `certs/` (mit `certs/ca/ca.key`).

Ohne den `MYBOXI_DEVICE_JWT_KEY` aus `.env` müssten alle Boxen neu gekoppelt und Spotify neu verbunden werden. Die gespeicherten Spotify-Zugänge sind mit einem daraus abgeleiteten Schlüssel verschlüsselt.

```text
docker compose exec -T db pg_dump -U myboxi -Fc myboxi > myboxi.dump
docker run --rm -v myboxi_assets:/a:ro -v "$PWD":/backup debian:trixie-slim \
  tar czf /backup/myboxi-assets.tgz -C /a .
```

Wiederherstellen auf einem frischen Server:
1. `.env` und `certs/` zurücklegen.
2. `docker compose up -d db` starten.
3. Die Datenbank einspielen: `docker compose exec -T db pg_restore -U myboxi -d myboxi --clean --if-exists < myboxi.dump`.
4. Die Audiodateien zurückspielen: `docker compose run --rm --no-deps -v "$PWD":/backup --entrypoint tar api xzf /backup/myboxi-assets.tgz -C /var/lib/myboxi-server/assets`.
5. `docker compose up -d` starten.

## 8. Was wo läuft

| Dienst | Aufgabe | Ports |
|---|---|---|
| `nginx` | HTTPS, liefert Audiodateien erst nach der Berechtigungsprüfung aus | 80, 443 |
| `api` | Web-UI und Geräte-API (uvicorn, nur über einen Unix-Socket zu nginx) | – |
| `worker` | Umwandeln der Uploads (ffmpeg), Aufräumen | – |
| `db` | PostgreSQL 17 | – |
| `migrate` | Datenbank-Migrationen beim Start | – |
| `certbot` | Let's Encrypt (Profil `letsencrypt`) | – |
| `mosquitto`, `mqtt-setup`, `mqtt` | Broker und MQTT-Dienst (Profil `mqtt`) | 8883 |

Die Daten liegen in Docker-Volumes (`myboxi_db`, `myboxi_assets`, `myboxi_data`, `myboxi_mosquitto`).

Alle Dienste nutzen dasselbe Image auf Basis von Debian 13, mit denselben Paketen wie die Installation ohne Docker. Sie laufen ohne unnötige Rechte; die Server-Dienste als eigener Nutzer.

Logs zeigt `docker compose logs -f`; je Dienst bleiben höchstens 5 × 10 MB. nginx schreibt keine Query-Strings ins Log (dort stehen Einmal-Tokens).

## Häufige Probleme

- **Die Box erreicht den Server nicht:**
  - Der Name in der Server-Adresse der Box muss genau zum Zertifikat und zu `MYBOXI_DOMAIN` passen.
  - Mit eigener CA muss die Box `myboxi-ca.pem` bekommen haben.
  - Das WLAN der Box muss den Namen auflösen können (DNS im Router, siehe Tipp in 1b).
- **Warnung im Browser:** Bei eigener CA die CA auf dem Gerät installieren (1c) oder die Warnung einmal bestätigen.
- **„CSRF-Prüfung fehlgeschlagen“ beim Anmelden oder Speichern:** `MYBOXI_BASE_URL` muss die Adresse sein, die im Browser steht, mit Port, falls nicht 443.
- **Nachsehen, was passiert:** `docker compose logs -f api nginx`, bei MQTT zusätzlich `mqtt mosquitto`.
