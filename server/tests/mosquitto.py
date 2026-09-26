"""A throwaway Mosquitto with dynamic security for the MQTT tests (skipped without mosquitto).

Plain TCP on 127.0.0.1 only: TLS is the broker configuration's job (deploy/mosquitto), the
code paths are the same.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

ADMIN = "myboxi-server"
ADMIN_PASSWORD = "broker-admin-test-password"
PLUGIN_DIRS = ("/usr/lib/x86_64-linux-gnu", "/usr/lib/aarch64-linux-gnu", "/usr/lib")


def available() -> bool:
    return shutil.which("mosquitto") is not None and shutil.which("mosquitto_ctrl") is not None


def _plugin() -> str:
    for d in PLUGIN_DIRS:
        path = Path(d) / "mosquitto_dynamic_security.so"
        if path.exists():
            return str(path)
    raise FileNotFoundError("mosquitto_dynamic_security.so")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass(frozen=True)
class Broker:
    host: str
    port: int


@contextmanager
def broker(tmp: Path) -> Generator[Broker]:
    tmp.mkdir(parents=True, exist_ok=True)
    dynsec = tmp / "dynsec.json"
    subprocess.run(
        ["mosquitto_ctrl", "dynsec", "init", str(dynsec), ADMIN, ADMIN_PASSWORD],
        check=True, capture_output=True,
    )  # fmt: skip
    port = _free_port()
    conf = tmp / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous false\npersistence false\n"
        f"plugin {_plugin()}\nplugin_opt_config_file {dynsec}\nlog_dest stderr\n"
        + ("user root\n" if os.geteuid() == 0 else "")
    )
    proc = subprocess.Popen(
        ["mosquitto", "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
    )
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) == 0:
                break
        time.sleep(0.05)
    try:
        yield Broker("127.0.0.1", port)
    finally:
        proc.terminate()
        proc.wait(timeout=10)
