# Box-Server auf Debian 13 betreiben

Diese Anleitung führt von einem frisch installierten Debian 13 (Trixie) bis zur laufenden Web-UI unter HTTPS.
Stack laut `CLAUDE.md`: nginx als Reverse-Proxy, PostgreSQL 17, ffmpeg, systemd – alles aus Debian. Einzige Fremdquelle ist `uv` (Python-Paketmanager), das als geprüftes Binary installiert wird.

Die Shell-Blöcke dieser Anleitung werden von `tools/debian13-check/run.sh` in einem Debian-13-Container ausgeführt. Die Anleitung ist damit selbst getestet.

## Voraussetzungen

- Debian 13, Root-Zugang (alle Befehle als `root`)
- Ein DNS-Eintrag (A/AAAA) für die gewünschte Domain, der auf den Server zeigt
- Offene Ports 80 und 443. PostgreSQL lauscht unter Debian nur lokal, weitere Ports braucht es nicht.
- Mindestens 1 GB RAM und Platz für die Audiodateien unter `/var/lib/box-server`

## 1. Variablen

Diese Werte anpassen. Die folgenden Schritte benutzen sie.

<!-- check: replace=vars -->
```bash
export BOX_DOMAIN=box.example.org
export BOX_ADMIN_EMAIL=ich@example.org
export BOX_TENANT_NAME="Familie Muster"
export BOX_REPO=https://github.com/benhartwich/box.git
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
adduser --system --group --home /var/lib/box-server --no-create-home box-server
install -d -o box-server -g box-server -m 0750 /var/lib/box-server
install -d -o box-server -g www-data -m 2750 /var/lib/box-server/assets
install -d -o box-server -g box-server -m 0750 /var/lib/box-server/tmp
install -d -o root -g box-server -m 0750 /etc/box-server
chmod 0751 /var/lib/box-server
```

## 5. Code

Der Code gehört `root` und ist für den Dienst nur lesbar. Python ist der System-Interpreter 3.13, uv lädt keinen eigenen herunter.

<!-- check: replace=clone -->
```bash
git clone --depth 1 "$BOX_REPO" /opt/box-server
```

```bash
cd /opt/box-server
UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --package box-server
install -m 0755 deploy/box-server-cli /usr/local/sbin/box-server
```

## 6. Datenbank

Die Datenbank ist über den Unix-Socket mit Peer-Authentifizierung erreichbar: Der Systemnutzer `box-server` meldet sich ohne Passwort als gleichnamige Datenbankrolle an.

```bash
runuser -u postgres -- createuser box-server
runuser -u postgres -- createdb --owner box-server box-server
```

## 7. Konfiguration

```bash
install -m 0640 -o root -g box-server /opt/box-server/deploy/box-server.env.example /etc/box-server/box-server.env
sed -i \
  -e "s|^BOX_SERVER_BASE_URL=.*|BOX_SERVER_BASE_URL=https://${BOX_DOMAIN}|" \
  -e "s|^BOX_SERVER_DEVICE_JWT_KEY=.*|BOX_SERVER_DEVICE_JWT_KEY=$(openssl rand -base64 48 | tr -d '\n')|" \
  /etc/box-server/box-server.env
```

Mail: Ohne SMTP (`BOX_SERVER_MAIL_BACKEND=log`) zeigt die Web-UI den Einladungslink einmal an, die einladende Person gibt ihn selbst weiter. Für den Versand per Mail `BOX_SERVER_MAIL_BACKEND=smtp` setzen und die `BOX_SERVER_SMTP_*`-Zeilen ausfüllen.

## 8. Schema und erster Owner

```bash
box-server migrate
```

Der erste Owner wird auf der Kommandozeile angelegt. Das Passwort wird abgefragt und muss mindestens 10 Zeichen haben. Weitere Personen lädt der Owner dann in der Web-UI ein.

<!-- check: replace=admin -->
```bash
box-server create-admin --email "$BOX_ADMIN_EMAIL" --tenant-name "$BOX_TENANT_NAME"
```

## 9. Dienste

`box-server-api.socket` legt den Unix-Socket `/run/box-server/api.sock` an (Gruppe `www-data`). systemd startet die API bei der ersten Verbindung. uvicorn lauscht nie auf einem Netzwerkport.

```bash
install -m 0644 /opt/box-server/deploy/systemd/box-server-api.socket \
  /opt/box-server/deploy/systemd/box-server-api.service \
  /opt/box-server/deploy/systemd/box-server-worker.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now box-server-api.socket box-server-worker.service
```

## 10. TLS-Zertifikat

certbot holt das Zertifikat über die Standard-Site von nginx. Das Debian-Paket richtet die automatische Erneuerung als systemd-Timer ein; der Deploy-Hook lädt nginx danach neu.

<!-- check: replace=certbot -->
```bash
certbot certonly --nginx --non-interactive --agree-tos -m "$BOX_ADMIN_EMAIL" -d "$BOX_DOMAIN" \
  --deploy-hook "systemctl reload nginx"
```

## 11. nginx

```bash
sed "s/box.example.org/${BOX_DOMAIN}/g" /opt/box-server/deploy/nginx/box-server.conf \
  > /etc/nginx/sites-available/box-server.conf
ln -sf /etc/nginx/sites-available/box-server.conf /etc/nginx/sites-enabled/box-server.conf
nginx -t
systemctl reload nginx
```

## 12. Prüfen

```bash
curl -fsS --resolve "${BOX_DOMAIN}:443:127.0.0.1" "https://${BOX_DOMAIN}/healthz" ${BOX_CURL_OPTS:-}
systemctl --no-pager --lines=0 status box-server-api.service box-server-worker.service
```

Danach ist die Web-UI unter `https://<Domain>/` erreichbar. Anmelden mit der E-Mail-Adresse und dem Passwort aus Schritt 8.

## Betrieb

**Logs:** strukturiert (JSON) im Journal. Secrets werden gefiltert, Query-Strings nicht geloggt.

```text
journalctl -u box-server-api -u box-server-worker -f
```

**Update:**

```text
cd /opt/box-server
git pull
UV_PYTHON_DOWNLOADS=never uv sync --frozen --no-dev --package box-server
box-server migrate
systemctl restart box-server-api.service box-server-worker.service
```

**Backup:** Datenbank und Asset-Verzeichnis gehören zusammen. Die Assets sind content-adressiert und ändern sich nie, ein inkrementelles `rsync` genügt.

```text
runuser -u postgres -- pg_dump --format=custom box-server > /var/backups/box-server.dump
rsync -a /var/lib/box-server/assets/ backup-host:/backups/box-server/assets/
```

**Aufräumen:** Der Worker löscht täglich um 03:17 Events älter als 30 Tage (SPEC §3.11), abgelaufene Sessions, Einladungen und Kopplungscodes sowie Audiodateien, auf die kein Inhalt mehr verweist.

**Upload-Größe:** `BOX_SERVER_MAX_UPLOAD_MB` gilt pro Datei. `client_max_body_size` in der nginx-Site begrenzt die gesamte Anfrage und muss dazu passen.

**Sicherheit:**
- Die API ist nur über den Unix-Socket erreichbar.
- Audiodateien liefert nginx ausschließlich aus einer `internal`-Location, nachdem die App die Berechtigung geprüft hat.
- Device-Secrets liegen nur als Argon2id-Hash in der Datenbank.
