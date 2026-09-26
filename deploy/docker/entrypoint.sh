#!/bin/sh
# Entry point of the self-hosting image (deploy/docker/compose.yaml, docs/selbst-hosten.md).
#   api | worker | migrate | mqtt | mqtt-setup   Myboxi server services (as myboxi-server)
#   nginx | mosquitto | certbot                  TLS front, MQTT broker, Let's Encrypt
#   myboxi-server ARGS                           any CLI command, e.g. create-admin
set -eu

DATA=/var/lib/myboxi-server
RUN_DIR=/run/myboxi-server
CERTS=/etc/myboxi/certs   # fullchain.pem, privkey.pem (and myboxi-ca.pem with an own CA)
TLS=/run/myboxi-tls       # copies the services read; renewed certificates are picked up

log() { echo "myboxi: $*" >&2; }

# Settings left empty in .env are unset, so the server's defaults apply.
for name in $(env | sed -n 's/^\(MYBOXI_SERVER_[A-Z0-9_]*\)=$/\1/p'); do unset "$name"; done

as_service() {
    umask 0027  # like the native units: other users read nothing (nginx reads via the group)
    export HOME="$DATA"
    exec setpriv --reuid=myboxi-server --regid=myboxi-server --init-groups -- "$@"
}

prepare_data() {
    install -d -o myboxi-server -g myboxi-server -m 0750 "$DATA" "$DATA/tmp"
    # setgid: new folders keep the group, so nginx (in the group) can read the audio files
    install -d -o myboxi-server -g myboxi-server -m 2750 "$DATA/assets"
    export TMPDIR="$DATA/tmp"
}

own_ca() {
    # With an own CA the MQTT service must trust it for the broker's certificate.
    if [ -f "$CERTS/myboxi-ca.pem" ]; then
        export MYBOXI_SERVER_MQTT_CA_FILE="$CERTS/myboxi-ca.pem"
    fi
}

cert_digest() { cat "$CERTS/fullchain.pem" "$CERTS/privkey.pem" 2>/dev/null | sha256sum; }

copy_certs() {  # $1: group that may read the key
    install -d -m 0750 -g "$1" "$TLS"
    if [ -f "$CERTS/fullchain.pem" ] && [ -f "$CERTS/privkey.pem" ]; then
        install -m 0644 "$CERTS/fullchain.pem" "$TLS/fullchain.pem"
        install -m 0640 -g "$1" "$CERTS/privkey.pem" "$TLS/privkey.pem"
    elif [ ! -f "$TLS/fullchain.pem" ]; then
        # Only so nginx can start and Let's Encrypt can reach it (certbot service).
        log "no certificate in $CERTS yet: using a temporary self-signed one"
        openssl req -x509 -newkey ec -pkeyopt ec_paramgen_curve:P-256 -nodes -days 7 \
            -subj "/CN=${MYBOXI_DOMAIN:-localhost}" -keyout "$TLS/privkey.pem" \
            -out "$TLS/fullchain.pem" 2>/dev/null
        chgrp "$1" "$TLS/privkey.pem"
        chmod 0640 "$TLS/privkey.pem"
    fi
}

watch_certs() {  # $1: command that reloads the service, $2: group that may read the key
    last=$(cert_digest)
    while sleep "${MYBOXI_CERT_CHECK_S:-300}"; do
        now=$(cert_digest)
        if [ "$now" != "$last" ]; then
            last=$now
            copy_certs "$2"
            log "certificate changed: reloading"
            sh -c "$1" || true
        fi
    done
}

render_nginx() {
    domain=${MYBOXI_DOMAIN:?set MYBOXI_DOMAIN in .env}
    sed -e "s/myboxi.example.org/${domain}/g" \
        -e "s#/etc/letsencrypt/live/[^/]*/#${TLS}/#" \
        -e "s#access_log /var/log/nginx/myboxi/app.access.log#access_log /dev/stdout#" \
        -e "s#error_log /var/log/nginx/myboxi/app.error.log#error_log /dev/stderr#" \
        -e "s#root /var/www/html;#root /var/www/certbot;#" \
        /opt/myboxi-server/deploy/nginx/myboxi-server.conf > /etc/nginx/sites-enabled/myboxi.conf
    if [ ! -f /proc/net/if_inet6 ]; then  # a container without IPv6
        sed -i '/listen \[::\]/d' /etc/nginx/sites-enabled/myboxi.conf
    fi
}

render_mosquitto() {
    plugin=$(find /usr/lib -name mosquitto_dynamic_security.so -print -quit)
    sed -e "s#/etc/mosquitto/certs/myboxi/#${TLS}/#" \
        -e "s#^plugin .*#plugin ${plugin}#" \
        /opt/myboxi-server/deploy/mosquitto/myboxi.conf > /etc/mosquitto/conf.d/myboxi.conf
    cat >> /etc/mosquitto/conf.d/myboxi.conf <<EOF
log_dest stderr
EOF
    # Debian's main configuration logs to a file; in the container stderr is enough.
    sed -i '/^log_dest file/d' /etc/mosquitto/mosquitto.conf
}

wait_for_broker() {
    for _ in $(seq 1 60); do
        if openssl s_client -connect "${MYBOXI_SERVER_MQTT_HOST:-mosquitto}:8883" \
            </dev/null >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    log "the broker does not answer"
    return 1
}

cmd=${1:-api}
[ $# -gt 0 ] && shift

case "$cmd" in
    api)
        prepare_data
        install -d -o myboxi-server -g myboxi-server -m 0755 "$RUN_DIR"
        rm -f "$RUN_DIR/api.sock"
        # uvicorn only on the Unix socket that nginx shares (CLAUDE.md), never on a port.
        as_service myboxi-server serve --uds "$RUN_DIR/api.sock" \
            --workers "${MYBOXI_API_WORKERS:-2}"
        ;;
    worker)
        prepare_data
        as_service myboxi-server worker
        ;;
    migrate)
        as_service myboxi-server migrate
        ;;
    mqtt)
        own_ca
        wait_for_broker
        as_service myboxi-server mqtt
        ;;
    mqtt-setup)
        own_ca
        wait_for_broker
        as_service myboxi-server mqtt-setup
        ;;
    myboxi-server)
        own_ca
        as_service myboxi-server "$@"
        ;;
    nginx)
        render_nginx
        install -d /var/www/certbot
        copy_certs www-data
        watch_certs "nginx -s reload" www-data &
        exec nginx -g "daemon off;"
        ;;
    mosquitto)
        : "${MYBOXI_MQTT_PASSWORD:?set MYBOXI_MQTT_PASSWORD in .env}"
        render_mosquitto
        install -d -o mosquitto -g mosquitto /run/mosquitto
        copy_certs mosquitto
        dynsec=/var/lib/mosquitto/dynamic-security.json
        if [ ! -f "$dynsec" ]; then
            # The server's admin account (docs/betrieb-debian13.md, "MQTT").
            mosquitto_ctrl dynsec init "$dynsec" myboxi-server "$MYBOXI_MQTT_PASSWORD" >/dev/null
        fi
        chown -R mosquitto:mosquitto /var/lib/mosquitto
        chmod 0600 "$dynsec"
        mosquitto -c /etc/mosquitto/mosquitto.conf &
        broker=$!
        # SIGHUP: Mosquitto reloads its certificate
        watch_certs "kill -HUP $broker" mosquitto &
        wait "$broker"
        ;;
    certbot)
        domain=${MYBOXI_DOMAIN:?set MYBOXI_DOMAIN in .env}
        email=${MYBOXI_LETSENCRYPT_EMAIL:?set MYBOXI_LETSENCRYPT_EMAIL in .env}
        live=/etc/letsencrypt/live/$domain
        while :; do
            # Due certificates only; nginx answers the challenge from the shared webroot.
            if certbot certonly --webroot -w /var/www/certbot -d "$domain" -m "$email" \
                --agree-tos --non-interactive --keep-until-expiring; then
                if ! cmp -s "$live/fullchain.pem" "$CERTS/fullchain.pem"; then
                    install -m 0644 "$live/fullchain.pem" "$CERTS/fullchain.pem"
                    install -m 0600 "$live/privkey.pem" "$CERTS/privkey.pem"
                    log "certificate saved; nginx and Mosquitto reload within minutes"
                fi
            fi
            sleep 12h
        done
        ;;
    *)
        log "unknown command: $cmd"
        exit 2
        ;;
esac
