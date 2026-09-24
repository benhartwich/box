"""Generated test media (CLAUDE.md: short generated sine/silence files, nothing copyrighted)."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path


def ffmpeg(*args: str) -> None:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", *args], check=True)


def sine_mp3(path: Path, seconds: float = 3.0, freq: int = 440) -> Path:
    ffmpeg("-f", "lavfi", "-i", f"sine=frequency={freq}:duration={seconds}", "-ac", "2",
           "-c:a", "libmp3lame", "-b:a", "128k", str(path))  # fmt: skip
    return path


def silence_wav(path: Path, seconds: float = 2.0) -> Path:
    ffmpeg("-f", "lavfi", "-i", f"anullsrc=r=44100:cl=mono:d={seconds}", str(path))
    return path


def color_png(path: Path, size: str = "640x480") -> Path:
    ffmpeg("-f", "lavfi", "-i", f"color=c=red:s={size}:d=1", "-frames:v", "1", str(path))
    return path


def probe(path: Path) -> dict[str, object]:
    out = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-print_format",
            "json",
            "-show_streams",
            "-show_format",
            str(path),
        ],
        check=True,
        capture_output=True,
    ).stdout
    data: dict[str, object] = json.loads(out)
    return data


def loudness(path: Path) -> float:
    err = subprocess.run(
        ["ffmpeg", "-hide_banner", "-nostdin", "-i", str(path), "-af", "ebur128=framelog=quiet",
         "-f", "null", "-"],
        check=True,
        capture_output=True,
    ).stderr.decode()  # fmt: skip
    line = next(ln for ln in reversed(err.splitlines()) if ln.strip().startswith("I:"))
    return float(line.split()[1])
