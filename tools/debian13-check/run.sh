#!/usr/bin/env bash
# Installs the server on a fresh Debian 13 container by executing docs/betrieb-debian13.md,
# then checks HTTPS, the web UI and the full device flow through nginx.
#   tools/debian13-check/run.sh [--keep]
# Binds only 127.0.0.1:18443; the host's nginx and services are not touched.
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/../.." && pwd)
HERE="$ROOT/tools/debian13-check"
NAME=box-debian13-check
IMAGE=box-debian13-check:latest
PORT=${BOX_CHECK_PORT:-18443}
KEEP=${1:-}
PASSWORD="check-$(head -c 12 /dev/urandom | base64 | tr -d '/+=')"

cleanup() {
  if [[ "$KEEP" != "--keep" ]]; then
    docker rm -f "$NAME" >/dev/null 2>&1 || true
  fi
}
trap cleanup EXIT

echo "== Image"
docker build -q -t "$IMAGE" "$HERE" >/dev/null
docker rm -f "$NAME" >/dev/null 2>&1 || true

echo "== Container (systemd)"
docker run -d --name "$NAME" --privileged --cgroupns=host \
  -v /sys/fs/cgroup:/sys/fs/cgroup:rw --tmpfs /run --tmpfs /run/lock \
  -v "$ROOT:/src:ro" -p "127.0.0.1:${PORT}:443" "$IMAGE" >/dev/null
for _ in $(seq 1 60); do
  state=$(docker exec "$NAME" systemctl is-system-running 2>/dev/null || true)
  [[ "$state" == running || "$state" == degraded ]] && break
  sleep 1
done
echo "   systemd: $state"

echo "== Anleitung ausführen (docs/betrieb-debian13.md)"
python3 "$HERE/extract.py" "$ROOT/docs/betrieb-debian13.md" "$HERE/replace" > "$HERE/.install.sh"
docker exec -i -e BOX_TEST_PASSWORD="$PASSWORD" "$NAME" bash < "$HERE/.install.sh" > "$HERE/.install.log" 2>&1 \
  || { tail -40 "$HERE/.install.log"; echo "Installation fehlgeschlagen (Log: tools/debian13-check/.install.log)"; exit 1; }
echo "   ok (Log: tools/debian13-check/.install.log)"

echo "== HTTPS von außen"
code=$(curl -sk -o /dev/null -w '%{http_code}' --resolve "box.test:${PORT}:127.0.0.1" "https://box.test:${PORT}/login")
[[ "$code" == 200 ]] || { echo "Web-UI antwortet mit $code"; exit 1; }
echo "   https://box.test:${PORT}/login → 200"

echo "== Abnahme gegen das Deployment"
cd "$ROOT"
uv run python "$HERE/smoke.py" "https://127.0.0.1:${PORT}" admin@box.test "$PASSWORD"

echo "== Dienste"
docker exec "$NAME" systemctl is-active box-server-api.socket box-server-api.service box-server-worker.service nginx postgresql
docker exec "$NAME" bash -c 'journalctl -u box-server-api -u box-server-worker --no-pager -o cat | grep -c "\"level\": \"ERROR\"" || true' \
  | sed 's/^/   Fehler im Journal: /'
echo "== Keine Secrets in Logs"
leaks=$(docker exec "$NAME" bash -c "journalctl --no-pager -o cat; cat /var/log/nginx/*.log" \
  | grep -cE "${PASSWORD}|poll_token=[A-Za-z0-9_-]{16}|token=[A-Za-z0-9_-]{30}|\\\$argon2id\\\$v" || true)
[[ "$leaks" == 0 ]] || { echo "   $leaks verdächtige Zeilen im Journal/nginx-Log"; exit 1; }
echo "   Journal und nginx-Log ohne Passwort, Poll-Token und Hashes"
echo "Alles grün."
