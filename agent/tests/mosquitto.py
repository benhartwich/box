"""A throwaway Mosquitto for the box's MQTT tests (skipped without mosquitto).

Password file and ACL instead of the server's dynamic security (tested in server/tests):
the box user may only use its own topics, like in production.
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

SERVER, SERVER_PASSWORD = "server", "server-test-password"


def available() -> bool:
    return shutil.which("mosquitto") is not None and shutil.which("mosquitto_passwd") is not None


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


@dataclass(frozen=True)
class Broker:
    host: str
    port: int


@contextmanager
def broker(tmp: Path, box: str, box_password: str) -> Generator[Broker]:
    tmp.mkdir(parents=True, exist_ok=True)
    passwords = tmp / "passwords"
    subprocess.run(["mosquitto_passwd", "-b", "-c", str(passwords), SERVER, SERVER_PASSWORD],
                   check=True, capture_output=True)  # fmt: skip
    subprocess.run(["mosquitto_passwd", "-b", str(passwords), box, box_password],
                   check=True, capture_output=True)  # fmt: skip
    acl = tmp / "acl"
    acl.write_text(
        f"user {SERVER}\ntopic readwrite myboxi/v1/#\n\n"
        f"user {box}\n"
        + "".join(f"topic write myboxi/v1/{box}/{leaf}\n"
                  for leaf in ("reported", "events", "cmd/ack", "online"))
        + "".join(f"topic read myboxi/v1/{box}/{leaf}\n" for leaf in ("notify", "cmd"))
    )  # fmt: skip
    for path in (passwords, acl):
        path.chmod(0o600)
    port = _free_port()
    conf = tmp / "mosquitto.conf"
    conf.write_text(
        f"listener {port} 127.0.0.1\nallow_anonymous false\npersistence false\n"
        f"password_file {passwords}\nacl_file {acl}\nlog_dest stderr\n"
        + ("user root\n" if os.geteuid() == 0 else "")
    )
    proc = subprocess.Popen(
        ["mosquitto", "-c", str(conf)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
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
