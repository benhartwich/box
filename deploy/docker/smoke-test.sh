#!/bin/sh
# Builds the self-hosting image and runs the whole stack with an own CA and MQTT, as a
# household would (docs/selbst-hosten.md). Used by CI; locally: deploy/docker/smoke-test.sh
# Ports 18080/18443/18883, only on 127.0.0.1; everything is removed afterwards.
set -eu

here=$(cd "$(dirname "$0")" && pwd)
work=$(mktemp -d)
project=myboxi-smoke-$$
domain=myboxi.test
https_port=${SMOKE_HTTPS_PORT:-18443}
mqtt_port=${SMOKE_MQTT_PORT:-18883}

CERTS_DIR=$work/certs "$here/own-ca.sh" "$domain" >/dev/null
chmod 0755 "$work/certs"
cat > "$work/env" <<EOF
MYBOXI_DOMAIN=$domain
MYBOXI_LISTEN=127.0.0.1
MYBOXI_BASE_URL=https://$domain:$https_port
MYBOXI_HTTP_PORT=${SMOKE_HTTP_PORT:-18080}
MYBOXI_HTTPS_PORT=$https_port
MYBOXI_MQTT_PORT=$mqtt_port
MYBOXI_CERTS_DIR=$work/certs
POSTGRES_PASSWORD=$(openssl rand -hex 24)
MYBOXI_DEVICE_JWT_KEY=$(openssl rand -base64 48 | tr -d '\n')
MYBOXI_MQTT_PASSWORD=$(openssl rand -hex 32)
COMPOSE_PROFILES=mqtt
EOF

dc() { docker compose -f "$here/compose.yaml" --env-file "$work/env" -p "$project" "$@"; }
cleanup() {
    status=$?
    if [ "$status" -ne 0 ]; then dc logs --no-color --tail 60 || true; fi
    dc down -v --remove-orphans >/dev/null 2>&1 || true
    rm -rf "$work"
    exit "$status"
}
trap cleanup EXIT
fail() { echo "smoke test FAILED: $*" >&2; exit 1; }
https() {  # verified against the own CA, like a box with the CA from the setup portal
    curl -fsS --cacert "$work/certs/myboxi-ca.pem" \
        --resolve "$domain:$https_port:127.0.0.1" "$@"
}

dc build -q
dc up -d 2>"$work/up.log" || { cat "$work/up.log" >&2; fail "compose up"; }
echo "waiting for https://$domain:$https_port/healthz"
for _ in $(seq 1 90); do
    https -o /dev/null "https://$domain:$https_port/healthz" 2>/dev/null && break
    sleep 2
done
https "https://$domain:$https_port/healthz" | grep -q '"ok"' || fail "no health answer"

# the first owner (docs/selbst-hosten.md)
printf 'smoke-test-password-123\n' | dc run --rm -T api myboxi-server create-admin \
    --email owner@example.org --tenant-name Smoke --password-stdin >/dev/null 2>&1 \
    || fail "create-admin"
https "https://$domain:$https_port/login" | grep -q "<form" || fail "login page"

# device API through nginx (SPEC §7.1)
device=$(cat /proc/sys/kernel/random/uuid)
key=$(openssl rand -base64 32 | tr '+/' '-_' | tr -d '=\n')
https -X POST -H 'content-type: application/json' \
    -d "{\"device_id\":\"$device\",\"hw_model\":\"rpi4\",\"agent_version\":\"0.7.0\",\"pairing_key\":\"$key\"}" \
    "https://$domain:$https_port/api/v1/pairing/start" | grep -q '"code"' || fail "pairing start"

# nginx (www-data) reads what the server writes, but only through the group
dc exec -T api setpriv --reuid=myboxi-server --regid=myboxi-server --init-groups -- \
    sh -c 'umask 0027; mkdir -p /var/lib/myboxi-server/assets/ab/cd &&
           echo probe > /var/lib/myboxi-server/assets/ab/cd/probe.opus' || fail "write asset"
dc exec -T nginx setpriv --reuid=www-data --regid=www-data --init-groups -- \
    cat /var/lib/myboxi-server/assets/ab/cd/probe.opus | grep -q probe || fail "nginx reads asset"

# MQTT: TLS with the own CA, no anonymous access, and the server's service is connected
answer=$(dc exec -T mosquitto mosquitto_sub -h "$domain" -p 8883 \
    --cafile /etc/myboxi/certs/myboxi-ca.pem -t 'myboxi/#' -W 3 2>&1 || true)
echo "$answer" | grep -q "not authorised" || fail "anonymous MQTT: $answer"
for _ in $(seq 1 30); do
    dc logs mqtt 2>/dev/null | grep -q "mqtt connected" && break
    sleep 2
done
dc logs mqtt 2>/dev/null | grep -q "mqtt connected" || fail "MQTT service not connected"
echo | openssl s_client -connect "127.0.0.1:$mqtt_port" -servername "$domain" \
    -CAfile "$work/certs/myboxi-ca.pem" -verify_return_error >/dev/null 2>&1 \
    || fail "broker certificate"

echo "smoke test passed"
