"""Facts about the box for pairing and ``reported`` (SPEC §6.4)."""

from __future__ import annotations

import shutil
from pathlib import Path

MODEL_FILE = Path("/proc/device-tree/model")
IMAGE_VERSION_FILE = Path("/etc/myboxi-image-version")


def detect_hw_model(model_file: Path = MODEL_FILE) -> str:
    """``rpi4``, ``rpi-zero2w`` … as in SPEC §3.3; ``linux`` elsewhere (sim, CI)."""
    try:
        model = model_file.read_text("utf-8", errors="replace").strip("\x00\n ")
    except OSError:
        return "linux"
    table = {
        "Raspberry Pi Zero 2 W": "rpi-zero2w",
        "Raspberry Pi 5": "rpi5",
        "Raspberry Pi 4": "rpi4",
        "Raspberry Pi 3": "rpi3",
        "Compute Module 4": "rpi-cm4",
    }
    for prefix, name in table.items():
        if model.startswith(prefix) or prefix in model:
            return name
    return "rpi" if model.startswith("Raspberry Pi") else "linux"


def image_version(path: Path = IMAGE_VERSION_FILE) -> str | None:
    try:
        return path.read_text("utf-8").strip()[:32] or None
    except OSError:
        return None


def free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free
