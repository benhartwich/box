# Myboxi-Server auf Debian 13 betreiben

Diese Anleitung führt von einem frisch installierten Debian 13 (Trixie) bis zur laufenden Web-UI unter HTTPS.
Stack laut `CLAUDE.md`: nginx als Reverse-Proxy, PostgreSQL 17, ffmpeg, systemd – alles aus Debian. Einzige Fremdquelle ist `uv` (Python-Paketmanager), das als geprüftes Binary installiert wird.

Die Shell-Blöcke dieser Anleitung werden von `tools/debian13-check/run.sh` in einem Debian-13-Container ausgeführt. Die Anleitung ist damit selbst getestet.

## Voraussetzungen

- Debian 13, Root-Zugang (alle Befehle als `root`)
- Ein DNS-Eintrag (A/AAAA) für die gewünschte Domain, der auf den Server zeigt
- Offene Ports 80 und 443. PostgreSQL lauscht unter Debian nur lokal, weitere Ports braucht es nicht.
- Mindestens 1 GB RAM und Platz für die Audiodateien unter `/var/lib/myboxi-server`

## 1. Variablen

Diese Werte anpassen. Die folgenden Schritte benutzen sie.

<!-- check: replace=vars -->
```bash
export MYBOXI_DOMAIN=myboxi.example.org
export MYBOXI_ADMIN_EMAIL=ich@example.org
export MYBOXI_TENANT_NAME="Familie Muster"
export MYBOXI_REPO=https://github.com/benhartwich/myboxi.git
```

## 2. Pakete

```bash
apt-get update
apt-get install -y --no-install-recommends \
  nginx postgresql-17 ffmpeg libpq5 python3 git ca-certificates curl openssl \
  certbot python3-certbot-nginx
```

## 3. uv installieren

`uv` ist nicht in Debian enthalten. Installiert wird ein fest versioniertes Release, dessen Prüfsumme vorher kontrolliert wird.

```bash
UV_VERSION=0.12.18
UV_ARCH=$(uname -m)-unknown-linux-gnu
cd /tmp
curl -fsSLO "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${UV_ARCH}.tar.gz"
curl -fsSLO "https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${UV_ARCH}.tar.gz.sha256"
sha256sum -c "uv-${UV_ARCH}.tar.gz.sha256"
tar xzf "uv-${UV_ARCH}.tar.gz"
install -m 0755 "uv-${UV_ARCH}/uv" "uv-${UV_ARCH}/uvx" /usr/local/bin/
uv --version
```

## 4. Dienstnutzer und Verzeichnisse

nginx (`www-data`) muss die Audiodateien lesen können, um sie nach der Berechtigungsprüfung der App auszuliefern. Deshalb gehört `assets` zur Gruppe `www-data`. Das setgid-Bit sorgt dafür, dass neue Unterordner die Gruppe erben.

```bash
adduser --system --group --home /var/lib/myboxi-server --no-create-home myboxi-server
install -d -o myboxi-server -g myboxi-server -m 0750 /var/lib/myboxi-server
install -d -o myboxi-server -g www-data -m 2750 /var/lib/myboxi-server/assets
install -d -o myboxi-server -g myboxi-server -m 0750 /var/lib/myboxi-server/tmp
install -d -o root -g myboxi-server -m 0750 /etc/myboxi-server
chmod 0751 /var/lib/myboxi-server
```

## 5. Code

Der Code gehört `root` und ist für den Dienst nur lesbar. Python ist der System-Interpreter 3.13, uv lädt keinen eigenen herunter.

<!-- check: replace=clone -->
```bash
git clone --depth 1 "$MYBOXI_REPO" /opt/myboxi-server
```

```bash
cd /opt/myboxi-server
UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --package myboxi-server
install -m 0755 deploy/myboxi-server-cli /usr/local/sbin/myboxi-server
```

## 6. Datenbank

Die Datenbank ist über den Unix-Socket mit Peer-Authentifizierung erreichbar: Der Systemnutzer `myboxi-server` meldet sich ohne Passwort als gleichnamige Datenbankrolle an.

```bash
runuser -u postgres -- createuser myboxi-server
runuser -u postgres -- createdb --owner myboxi-server myboxi-server
```

## 7. Konfiguration

```bash
install -m 0640 -o root -g myboxi-server /opt/myboxi-server/deploy/myboxi-server.env.example /etc/myboxi-server/myboxi-server.env
sed -i \
  -e "s|^MYBOXI_SERVER_BASE_URL=.*|MYBOXI_SERVER_BASE_URL=https://${MYBOXI_DOMAIN}|" \
  -e "s|^MYBOXI_SERVER_DEVICE_JWT_KEY=.*|MYBOXI_SERVER_DEVICE_JWT_KEY=$(openssl rand -base64 48 | tr -d '\n')|" \
  /etc/myboxi-server/myboxi-server.env
```

Mail: Ohne SMTP (`MYBOXI_SERVER_MAIL_BACKEND=log`) zeigt die Web-UI den Einladungslink einmal an, die einladende Person gibt ihn selbst weiter. Für den Versand per Mail `MYBOXI_SERVER_MAIL_BACKEND=smtp` setzen und die `MYBOXI_SERVER_SMTP_*`-Zeilen ausfüllen.

## 8. Schema und erster Owner

```bash
myboxi-server migrate
```

Der erste Owner wird auf der Kommandozeile angelegt. Das Passwort wird abgefragt und muss mindestens 10 Zeichen haben. Weitere Personen lädt der Owner dann in der Web-UI ein; angemeldete Nutzer legen weitere Haushalte selbst an (höchstens 10 je Nutzer).

<!-- check: replace=admin -->
```bash
myboxi-server create-admin --email "$MYBOXI_ADMIN_EMAIL" --tenant-name "$MYBOXI_TENANT_NAME"
```

## 9. Dienste

`myboxi-server-api.socket` legt den Unix-Socket `/run/myboxi-server/api.sock` an (Gruppe `www-data`). systemd startet die API bei der ersten Verbindung. uvicorn lauscht nie auf einem Netzwerkport.

```bash
install -m 0644 /opt/myboxi-server/deploy/systemd/myboxi-server-api.socket \
  /opt/myboxi-server/deploy/systemd/myboxi-server-api.service \
  /opt/myboxi-server/deploy/systemd/myboxi-server-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now myboxi-server-api.socket myboxi-server-worker.service
```

## 10. TLS-Zertifikat

certbot holt das Zertifikat über die Standard-Site von nginx. Das Debian-Paket richtet die automatische Erneuerung als systemd-Timer ein; der Deploy-Hook lädt nginx danach neu.

<!-- check: replace=certbot -->
```bash
certbot certonly --nginx --non-interactive --agree-tos -m "$MYBOXI_ADMIN_EMAIL" -d "$MYBOXI_DOMAIN" \
  --deploy-hook "systemctl reload nginx"
```

## 11. nginx

Die Zugriffsprotokolle (mit IP-Adressen) liegen in `/var/log/nginx/myboxi/` und werden nach 14 Tagen gelöscht (`deploy/logrotate/myboxi-nginx`).

```bash
install -d -m 0750 -o root -g adm /var/log/nginx/myboxi
install -D -m 0644 /opt/myboxi-server/deploy/logrotate/myboxi-nginx /etc/logrotate.d/myboxi-nginx
sed "s/myboxi.example.org/${MYBOXI_DOMAIN}/g" /opt/myboxi-server/deploy/nginx/myboxi-server.conf \
  > /etc/nginx/sites-available/myboxi-server.conf
ln -sf /etc/nginx/sites-available/myboxi-server.conf /etc/nginx/sites-enabled/myboxi-server.conf
nginx -t
systemctl reload nginx
```

## 12. Prüfen

```bash
curl -fsS --resolve "${MYBOXI_DOMAIN}:443:127.0.0.1" "https://${MYBOXI_DOMAIN}/healthz" ${MYBOXI_CURL_OPTS:-}
systemctl --no-pager --lines=0 status myboxi-server-api.service myboxi-server-worker.service
```

Danach ist die Web-UI unter `https://<Domain>/` erreichbar. Anmelden mit der E-Mail-Adresse und dem Passwort aus Schritt 8.

## Betrieb

**Logs:** strukturiert (JSON) im Journal. Secrets werden gefiltert, Query-Strings nicht geloggt.

```text
journalctl -u myboxi-server-api -u myboxi-server-worker -f
```

**Update:**

```text
cd /opt/myboxi-server
git pull
UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --package myboxi-server
myboxi-server migrate
systemctl restart myboxi-server-api.service myboxi-server-worker.service
```

**Backup:** Datenbank und Asset-Verzeichnis gehören zusammen. Die Assets sind content-adressiert und ändern sich nie, ein inkrementelles `rsync` genügt.

```text
runuser -u postgres -- pg_dump --format=custom myboxi-server > /var/backups/myboxi-server.dump
rsync -a /var/lib/myboxi-server/assets/ backup-host:/backups/myboxi-server/assets/
```

**Aufräumen:** Der Worker löscht täglich um 03:17 Events älter als 30 Tage (SPEC §3.11), abgelaufene Sessions, Einladungen und Kopplungscodes sowie Audiodateien, auf die kein Inhalt mehr verweist.

**Box-Updates:** `MYBOXI_SERVER_UPDATE_MANIFEST_URL` zeigt auf das Update-Manifest (Vorgabe: Kanal `channel-stable` auf GitHub). Die App zeigt damit auf der Box-Seite, ob eine Box aktuell ist; leer schaltet die Anzeige der neuesten Version ab. Siehe `docs/updates.md`.

**Links im Einrichtungs-Assistenten:** `MYBOXI_SERVER_IMAGE_URL` (Download der Box-Images, Vorgabe: GitHub-Releases) und `MYBOXI_SERVER_DOCS_URL` (Hardware-Anleitung). Für eigene Builds oder Forks anpassen.

**Box gestalten:** Die Seite `/gestalten` ist öffentlich und braucht keine Einstellung. Vorschauen und Druckdateien entstehen im Server-Prozess (etwa 30 ms je Gehäuse) und liegen in einem Zwischenspeicher von `MYBOXI_SERVER_CASE_CACHE_MB` (Vorgabe 64). Anfragen für gedruckte Gehäuse gibt es erst, wenn `MYBOXI_SERVER_ORDER_NOTIFY_EMAIL` gesetzt ist; bestätigte Anfragen gehen per Mail dorthin (SMTP nötig). Verwaltung: `myboxi-server case-requests list|show|status|delete`. Siehe `docs/gehaeuse.md`.

**Upload-Größe:** `MYBOXI_SERVER_MAX_UPLOAD_MB` gilt pro Datei. `client_max_body_size` in der nginx-Site begrenzt die gesamte Anfrage und muss dazu passen.

**Sicherheit:**
- Die API ist nur über den Unix-Socket erreichbar.
- Audiodateien liefert nginx ausschließlich aus einer `internal`-Location, nachdem die App die Berechtigung geprüft hat.
- Device-Secrets liegen nur als Argon2id-Hash in der Datenbank.
