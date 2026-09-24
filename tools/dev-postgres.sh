#!/usr/bin/env bash
# Local development PostgreSQL 17 in Docker, bound to 127.0.0.1 only.
# Writes connection URLs to .dev/env (git-ignored); source it before running the server or tests.
set -euo pipefail

NAME=${BOX_DEV_PG_NAME:-box-dev-postgres}
PORT=${BOX_DEV_PG_PORT:-55433}
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
    -e POSTGRES_USER=box -e POSTGRES_PASSWORD="$PASSWORD" -e POSTGRES_DB=box \
    -v box-dev-pgdata:/var/lib/postgresql/data \
    postgres:17 >/dev/null
fi
docker start "$NAME" >/dev/null

for _ in $(seq 1 30); do
  docker exec "$NAME" pg_isready -U box -d box >/dev/null 2>&1 && break
  sleep 1
done

(umask 077 && cat > "$ENV_FILE" <<ENV
export BOX_SERVER_DATABASE_URL=postgresql://box:${PASSWORD}@127.0.0.1:${PORT}/box
export BOX_SERVER_TEST_DATABASE_URL=postgresql://box:${PASSWORD}@127.0.0.1:${PORT}/box_test
export BOX_SERVER_ENV=dev
export BOX_SERVER_DEVICE_JWT_KEY=$(cat "$ROOT/.dev/jwt-key")
export BOX_SERVER_DATA_DIR=$ROOT/.dev/data
export BOX_SERVER_SESSION_COOKIE_SECURE=false
export BOX_SERVER_LOG_FORMAT=console
ENV
)
docker exec "$NAME" psql -U box -d box -tAc "SELECT 1 FROM pg_database WHERE datname='box_test'" | grep -q 1 \
  || docker exec "$NAME" createdb -U box box_test
echo "PostgreSQL ready on 127.0.0.1:${PORT}; run: source .dev/env"
