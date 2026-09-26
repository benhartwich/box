# Myboxi server for self-hosting with Docker Compose (docs/selbst-hosten.md).
# One Debian 13 image for every service: API, worker, MQTT service, nginx, Mosquitto,
# certbot. The same packages as the native installation (docs/betrieb-debian13.md).
# Build context: the repository root.
FROM debian:trixie-slim

ARG UV_VERSION=0.12.18
ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    UV_PYTHON_DOWNLOADS=never \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_NO_CACHE=1

RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      python3 ffmpeg libpq5 ca-certificates curl openssl tini \
      nginx mosquitto mosquitto-clients certbot \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /etc/nginx/sites-enabled/default \
 && ln -sf /dev/stdout /var/log/nginx/access.log \
 && ln -sf /dev/stderr /var/log/nginx/error.log

# uv is not in Debian: a pinned release, checked against its published checksum.
RUN set -eu; arch="$(uname -m)-unknown-linux-gnu"; cd /tmp; \
    base="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}"; \
    curl -fsSLO "${base}/uv-${arch}.tar.gz"; curl -fsSLO "${base}/uv-${arch}.tar.gz.sha256"; \
    sha256sum -c "uv-${arch}.tar.gz.sha256"; tar xzf "uv-${arch}.tar.gz"; \
    install -m 0755 "uv-${arch}/uv" /usr/local/bin/uv; rm -rf /tmp/uv-*

# The service user; nginx (www-data) joins its group to read the audio files (X-Accel).
RUN addgroup --system --gid 1500 myboxi-server \
 && adduser --system --uid 1500 --ingroup myboxi-server --home /var/lib/myboxi-server \
      --no-create-home myboxi-server \
 && usermod -aG myboxi-server www-data

# Dependencies first (cached while only the code changes), then the code. Code and venv
# belong to root; the services can only read them.
WORKDIR /opt/myboxi-server
COPY pyproject.toml uv.lock ./
COPY packages/protocol/pyproject.toml packages/protocol/
COPY server/pyproject.toml server/
COPY agent/pyproject.toml agent/
COPY hardware/case/pyproject.toml hardware/case/
RUN uv sync --frozen --no-dev --package myboxi-server --no-install-workspace
COPY . .
RUN uv sync --frozen --no-dev --package myboxi-server \
 && chmod 0755 deploy/docker/entrypoint.sh

ENV PATH=/opt/myboxi-server/.venv/bin:$PATH
# tini -g: signals reach the whole process group (nginx and Mosquitto run beside a loop).
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/opt/myboxi-server/deploy/docker/entrypoint.sh"]
CMD ["api"]
