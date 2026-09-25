#!/usr/bin/env bash
# Builds the Myboxi image from Raspberry Pi OS Lite (arm64, Trixie).
# Runs as root on an arm64 host (GitHub runner ubuntu-24.04-arm): native chroot, no emulation.
#
#   sudo PROMPTS_DIR=build/prompts image/build.sh
#
# Output: build/myboxi-<version>.img.xz and .sha256, and the agent update bundle
#         build/myboxi-agent-<agent version>-arm64.tar.xz (SPEC v0.7 §11.1)
set -euo pipefail

ROOT=$(cd "$(dirname "$0")/.." && pwd)
OUT=${OUT:-$ROOT/build}
PROMPTS_DIR=${PROMPTS_DIR:-$OUT/prompts}
VERSION=${MYBOXI_IMAGE_VERSION:-$(date -u +%Y.%m.%d)-$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || echo dev)}
EXTRA_MB=${EXTRA_MB:-1536}
AGENT_VERSION=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$ROOT/agent/pyproject.toml" | head -n 1)

# Pinned base image (update URL and checksum together).
BASE_URL=https://downloads.raspberrypi.com/raspios_lite_arm64/images/raspios_lite_arm64-2026-09-15/2026-09-15-raspios-trixie-arm64-lite.img.xz
BASE_SHA256=cdf4f3bfac35ae947b46e4e767f935453810549779ac3290e05a6754aee627e5

UV_BIN=${UV_BIN:-$(command -v uv)}
IMG="$OUT/myboxi-$VERSION.img"
MNT="$OUT/mnt"
LOOP=""

cleanup() {
    set +e
    for m in dev/pts dev sys proc boot/firmware ""; do
        mountpoint -q "$MNT/$m" && umount -l "$MNT/$m"
    done
    [ -n "$LOOP" ] && losetup -d "$LOOP"
}
trap cleanup EXIT

[ "$(uname -m)" = "aarch64" ] || { echo "needs an arm64 host"; exit 1; }
[ -f "$PROMPTS_DIR/tone_start.opus" ] || { echo "prompts missing in $PROMPTS_DIR"; exit 1; }
[ -f "$PROMPTS_DIR/NOTICE.txt" ] || { echo "voice notice missing in $PROMPTS_DIR"; exit 1; }
mkdir -p "$OUT/cache" "$MNT"

echo "== base image"
BASE="$OUT/cache/$(basename "$BASE_URL")"
[ -f "$BASE" ] || curl -fL --retry 3 -o "$BASE" "$BASE_URL"
echo "$BASE_SHA256  $BASE" | sha256sum -c -
xz -dc "$BASE" > "$IMG"

echo "== grow root partition by ${EXTRA_MB} MB"
truncate -s "+${EXTRA_MB}M" "$IMG"
parted -s "$IMG" resizepart 2 100%
LOOP=$(losetup -fP --show "$IMG")
e2fsck -pf "${LOOP}p2" || [ $? -le 1 ]
resize2fs "${LOOP}p2"

echo "== mount"
mount "${LOOP}p2" "$MNT"
mount "${LOOP}p1" "$MNT/boot/firmware"
for m in proc sys dev dev/pts; do mount --bind "/$m" "$MNT/$m"; done
if [ -e "$MNT/etc/resolv.conf" ] || [ -L "$MNT/etc/resolv.conf" ]; then
    mv "$MNT/etc/resolv.conf" "$MNT/etc/resolv.conf.myboxi-orig"
fi
cp -L /etc/resolv.conf "$MNT/etc/resolv.conf"
printf '#!/bin/sh\nexit 101\n' > "$MNT/usr/sbin/policy-rc.d"
chmod +x "$MNT/usr/sbin/policy-rc.d"

echo "== agent $AGENT_VERSION: source (build only), uv, prompts, system files"
# Everything copied into the image belongs to root, never to the build user (tar keeps owners).
TAR_ROOT=(--owner=0 --group=0 --numeric-owner)
RELEASE="/opt/myboxi-agent/releases/$AGENT_VERSION"
install -d "$MNT/tmp/myboxi-src" "$MNT$RELEASE/prompts"
tar -C "$ROOT" "${TAR_ROOT[@]}" -cf - pyproject.toml uv.lock .python-version packages/protocol agent \
    server/pyproject.toml | tar -C "$MNT/tmp/myboxi-src" -xf -
install -m 0755 "$UV_BIN" "$MNT/usr/local/bin/uv"
install -m 0644 "$PROMPTS_DIR"/*.opus "$PROMPTS_DIR/NOTICE.txt" "$MNT$RELEASE/prompts/"
# --no-overwrite-dir: /, /etc, /usr … keep owner and mode of the base image.
tar -C "$ROOT/image/files" "${TAR_ROOT[@]}" -cf - . | tar -C "$MNT" --no-overwrite-dir -xf -
[ -f "$MNT/etc/myboxi-agent/myboxi-agent.env" ] || { echo "myboxi-agent.env missing"; exit 1; }
cat "$ROOT/image/config.txt.append" >> "$MNT/boot/firmware/config.txt"
# HDMI audio off too, so PipeWire's only sink is the MAX98357A.
sed -i 's/^dtoverlay=vc4-kms-v3d$/dtoverlay=vc4-kms-v3d,noaudio/' "$MNT/boot/firmware/config.txt"
echo "$VERSION" > "$MNT/etc/myboxi-image-version"

echo "== chroot"
chroot "$MNT" /usr/bin/env RELEASE="$RELEASE" AGENT_VERSION="$AGENT_VERSION" \
    /bin/bash -euxo pipefail <<'CHROOT'
export DEBIAN_FRONTEND=noninteractive LC_ALL=C.UTF-8
apt-get update
apt-get install -y --no-install-recommends \
    mpv pipewire wireplumber pipewire-alsa python3 python3-lgpio python3-rpi-lgpio \
    i2c-tools dnsmasq-base polkitd openssl unattended-upgrades nftables \
    build-essential python3-dev

# Service user: GPIO, I2C and audio; its user session (linger) runs PipeWire and the agent.
useradd --system --create-home --home-dir /var/lib/myboxi --shell /usr/sbin/nologin \
    --groups gpio,i2c,audio myboxi
install -d -m 0755 /var/lib/systemd/linger
touch /var/lib/systemd/linger/myboxi
install -d -o myboxi -g myboxi -m 0700 /var/lib/myboxi/prompts

# Agent venv from the pinned lock, not editable: the release directory is self-contained, so
# the updater can replace it as a whole (SPEC v0.7 §11.1). System site packages: lgpio, RPi.GPIO.
cd /tmp/myboxi-src
export UV_PYTHON_DOWNLOADS=never UV_CACHE_DIR=/tmp/uv-cache UV_PROJECT_ENVIRONMENT="$RELEASE/.venv"
uv venv --system-site-packages --python /usr/bin/python3 "$RELEASE/.venv"
uv sync --frozen --no-dev --no-editable --package myboxi-agent --extra hw
ln -sfn "releases/$AGENT_VERSION" /opt/myboxi-agent/current
cd /
rm -rf /tmp/uv-cache /tmp/myboxi-src /usr/local/bin/uv
[ "$(/opt/myboxi-agent/current/.venv/bin/myboxi-agent version)" = "myboxi-agent $AGENT_VERSION" ]

apt-get purge -y build-essential python3-dev
apt-get autoremove -y
apt-get clean
rm -rf /var/lib/apt/lists/*

systemctl enable myboxi-firstboot.service myboxi-updater.timer
# Firewall: inbound only from the home network (SPEC v0.9 §10, /etc/nftables.conf).
systemctl enable nftables.service
# Spotify Connect announces itself over mDNS on UDP 5353; avahi would hold that port and the
# box would silently not show up in the Spotify app. The box needs no .local name.
systemctl mask avahi-daemon.service avahi-daemon.socket
install -d -m 0700 /var/lib/myboxi-updater
systemctl --global enable pipewire.socket wireplumber.service
# The agent only in the session of "myboxi" (the unit also has ConditionUser=myboxi).
install -d /var/lib/myboxi/.config/systemd/user/default.target.wants \
    /var/lib/myboxi/.config/systemd/user/timers.target.wants
ln -sf /usr/lib/systemd/user/myboxi-agent.service \
    /var/lib/myboxi/.config/systemd/user/default.target.wants/myboxi-agent.service
# Spotify (SPEC v0.9 §8.1): the daily Soloist update check; it does nothing while Spotify is off.
ln -sf /usr/lib/systemd/user/myboxi-soloist-update.timer \
    /var/lib/myboxi/.config/systemd/user/timers.target.wants/myboxi-soloist-update.timer
chown -R myboxi:myboxi /var/lib/myboxi

# Nothing may belong to a user that does not exist on the box (e.g. the build user).
orphans=$(find / -xdev \( -nouser -o -nogroup \) -print)
[ -z "$orphans" ] || { echo "files without owner on the box:"; echo "$orphans"; exit 1; }

# Self-test without hardware or network.
runuser -u myboxi -- env MYBOXI_AGENT_PROMPTS_DIR=/opt/myboxi-agent/current/prompts \
    /opt/myboxi-agent/current/.venv/bin/myboxi-agent --data-dir /tmp/doctor doctor --offline
rm -rf /tmp/doctor
openssl version
CHROOT

echo "== update bundle"
BUNDLE="$OUT/myboxi-agent-$AGENT_VERSION-arm64.tar.xz"
tar -C "$MNT/opt/myboxi-agent/releases" "${TAR_ROOT[@]}" -cf - "$AGENT_VERSION" | xz -T0 -6 > "$BUNDLE"
(cd "$OUT" && sha256sum "$(basename "$BUNDLE")" > "$(basename "$BUNDLE").sha256")
ls -la "$BUNDLE"

rm -f "$MNT/usr/sbin/policy-rc.d" "$MNT/etc/resolv.conf"
if [ -e "$MNT/etc/resolv.conf.myboxi-orig" ] || [ -L "$MNT/etc/resolv.conf.myboxi-orig" ]; then
    mv "$MNT/etc/resolv.conf.myboxi-orig" "$MNT/etc/resolv.conf"
fi

echo "== finish"
cleanup
trap - EXIT
xz -T0 -6 -f "$IMG"
(cd "$OUT" && sha256sum "$(basename "$IMG").xz" > "$(basename "$IMG").xz.sha256")
ls -la "$IMG.xz"
echo "version $VERSION, agent $AGENT_VERSION"
