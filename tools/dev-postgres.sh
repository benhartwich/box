#!/usr/bin/env bash
# Local development PostgreSQL 17 in Docker, bound to 127.0.0.1 only.
# Writes connection URLs to .dev/env (git-ignored); source it before running the server or tests.
set -euo pipefail

NAME=${MYBOXI_DEV_PG_NAME:-myboxi-dev-postgres}
PORT=${MYBOXI_DEV_PG_PORT:-55433}
ROOT=$(cd "$(dirname "$0")/.." && pwd)
ENV_FILE="$ROOT/.dev/env"

mkdir -p "$ROOT/.dev"
if [[ ! -f "$ROOT/.dev/pg-password" ]]; then
  (umask 077 && head -c 24 /dev/urandom | base64 | tr -d '/+=' > "$ROOT/.dev/pg-password")
fi
PASSWORD=$(cat "$ROOT/.dev/pg-password")
if [[ ! -f "$ROOT/.dev/jwt-key" ]]; then
  (umask 077 && head -c 48 /dev/urandom | base64 | tr -d '/+=\n' > "$ROOT/.dev/jwt-key")
fi

if ! docker container inspect "$NAME" >/dev/null 2>&1; then
  docker run -d --name "$NAME" --restart unless-stopped \
    -p "127.0.0.1:${PORT}:5432" \
    -e POSTGRES_USER=myboxi -e POSTGRES_PASSWORD="$PASSWORD" -e POSTGRES_DB=myboxi \
    -v myboxi-dev-pgdata:/var/lib/postgresql/data \
    postgres:17 >/dev/null
fi
docker start "$NAME" >/dev/null

for _ in $(seq 1 30); do
  docker exec "$NAME" pg_isready -U myboxi -d myboxi >/dev/null 2>&1 && break
  sleep 1
done

(umask 077 && cat > "$ENV_FILE" <<ENV
export MYBOXI_SERVER_DATABASE_URL=postgresql://myboxi:${PASSWORD}@127.0.0.1:${PORT}/myboxi
export MYBOXI_SERVER_TEST_DATABASE_URL=postgresql://myboxi:${PASSWORD}@127.0.0.1:${PORT}/myboxi_test
export MYBOXI_SERVER_ENV=dev
export MYBOXI_SERVER_DEVICE_JWT_KEY=$(cat "$ROOT/.dev/jwt-key")
export MYBOXI_SERVER_DATA_DIR=$ROOT/.dev/data
export MYBOXI_SERVER_SESSION_COOKIE_SECURE=false
export MYBOXI_SERVER_LOG_FORMAT=console
ENV
)
docker exec "$NAME" psql -U myboxi -d myboxi -tAc "SELECT 1 FROM pg_database WHERE datname='myboxi_test'" | grep -q 1 \
  || docker exec "$NAME" createdb -U myboxi myboxi_test
echo "PostgreSQL ready on 127.0.0.1:${PORT}; run: source .dev/env"
