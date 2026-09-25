"""NetworkManager via nmcli. Arguments are lists (no shell); passwords are never logged."""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable, Sequence
from dataclasses import dataclass

log = logging.getLogger(__name__)

HOTSPOT = "myboxi-setup"
HOTSPOT_ADDRESS = "10.42.0.1"
IFACE = "wlan0"


@dataclass(frozen=True)
class Result:
    code: int
    out: str


Runner = Callable[[Sequence[str], float], Result]


def run_nmcli(args: Sequence[str], timeout: float = 30) -> Result:
    try:
        proc = subprocess.run(  # noqa: S603 - fixed program, argument list
            ["/usr/bin/nmcli", *args], capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Result(1, str(exc))
    return Result(proc.returncode, proc.stdout)


@dataclass(frozen=True)
class Network:
    ssid: str
    signal: int
    secured: bool


def split_terse(line: str) -> list[str]:
    """nmcli -t escapes ':' and '\\' with a backslash."""
    fields: list[str] = []
    current, escaped = "", False
    for ch in line:
        if escaped:
            current += ch
            escaped = False
        elif ch == "\\":
            escaped = True
        elif ch == ":":
            fields.append(current)
            current = ""
        else:
            current += ch
    fields.append(current)
    return fields


class NetworkManager:
    def __init__(self, runner: Runner = run_nmcli, iface: str = IFACE) -> None:
        self.run = runner
        self.iface = iface

    def scan(self) -> list[Network]:
        r = self.run(
            ["-t", "-f", "SSID,SIGNAL,SECURITY", "dev", "wifi", "list", "--rescan", "yes"], 30
        )
        best: dict[str, Network] = {}
        for line in r.out.splitlines():
            parts = split_terse(line)
            if len(parts) < 3 or not parts[0]:
                continue
            try:
                signal = int(parts[1])
            except ValueError:
                signal = 0
            net = Network(parts[0], signal, parts[2] not in ("", "--"))
            if net.ssid not in best or best[net.ssid].signal < signal:
                best[net.ssid] = net
        return sorted(best.values(), key=lambda n: -n.signal)

    def wifi_configured(self) -> bool:
        r = self.run(["-t", "-f", "NAME,TYPE", "connection", "show"], 10)
        return any(
            p[1] == "802-11-wireless" and p[0] != HOTSPOT
            for p in (split_terse(line) for line in r.out.splitlines())
            if len(p) >= 2
        )

    def online(self) -> bool:
        r = self.run(["-t", "-f", "STATE", "general"], 10)
        return r.out.strip().startswith("connected")

    def ethernet_connected(self) -> bool:
        r = self.run(["-t", "-f", "TYPE,STATE", "device"], 10)
        return any(line == "ethernet:connected" for line in r.out.splitlines())

    def start_hotspot(self, ssid: str) -> bool:
        self.run(["connection", "delete", HOTSPOT], 15)
        add = self.run(
            [
                "connection", "add", "type", "wifi", "ifname", self.iface, "con-name", HOTSPOT,
                "autoconnect", "no", "ssid", ssid, "802-11-wireless.mode", "ap",
                "802-11-wireless.band", "bg", "ipv4.method", "shared",
                "ipv4.addresses", f"{HOTSPOT_ADDRESS}/24", "ipv6.method", "disabled",
            ],
            15,
        )  # fmt: skip
        up = self.run(["connection", "up", HOTSPOT], 30) if add.code == 0 else add
        if up.code != 0:
            log.error("hotspot failed", extra={"out": up.out[-300:]})
        return up.code == 0

    def stop_hotspot(self) -> None:
        self.run(["connection", "down", HOTSPOT], 15)
        self.run(["connection", "delete", HOTSPOT], 15)

    def connect_wifi(self, ssid: str, password: str) -> bool:
        """Create (or replace) the connection for ``ssid`` and bring it up."""
        name = f"myboxi-wifi-{ssid}"[:64]
        self.run(["connection", "delete", name], 15)
        args = [
            "connection", "add", "type", "wifi", "ifname", self.iface, "con-name", name,
            "ssid", ssid, "connection.autoconnect", "yes",
        ]  # fmt: skip
        if password:
            args += ["wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.psk", password]
        if self.run(args, 15).code != 0:
            log.error("could not add Wi-Fi connection", extra={"ssid": ssid})
            return False
        up = self.run(["--wait", "45", "connection", "up", name], 60)
        if up.code != 0:
            self.run(["connection", "delete", name], 15)
            log.warning("Wi-Fi connection failed", extra={"ssid": ssid})
            return False
        return True


def network_name(serial: str | None) -> str:
    """``Myboxi-NNNN``: four digits the box can read out (SPEC v0.5 §9.3)."""
    try:
        number = int(serial or "", 16) % 10000
    except ValueError:
        number = sum((serial or "myboxi").encode()) % 10000
    return f"Myboxi-{number:04d}"


def read_serial(cpuinfo: str = "/proc/cpuinfo") -> str | None:
    try:
        with open(cpuinfo, encoding="ascii", errors="replace") as fh:  # noqa: PTH123
            for line in fh:
                if line.startswith("Serial"):
                    return line.split(":", 1)[1].strip()
    except OSError:
        return None
    return None
