# A "fresh Debian 13" with systemd as PID 1, for testing docs/betrieb-debian13.md.
# policy-rc.d is removed: like on a real install, packages start their services.
FROM debian:trixie
ENV container=docker DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends systemd systemd-sysv dbus \
 && rm -rf /var/lib/apt/lists/* \
 && rm -f /usr/sbin/policy-rc.d \
 && systemctl mask getty.target systemd-logind.service console-getty.service \
      systemd-remount-fs.service sys-kernel-debug.mount sys-kernel-tracing.mount
STOPSIGNAL SIGRTMIN+3
CMD ["/sbin/init"]
