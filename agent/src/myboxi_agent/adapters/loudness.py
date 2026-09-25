"""Loudness of downloaded podcast episodes (SPEC v0.8 §8.2), measured with mpv.

mpv decodes the file as fast as it can into the null output, with ffmpeg's ``ebur128`` filter
in its audio chain; the filter's summary ("I: -19.3 LUFS") arrives in mpv's log. No extra
package: mpv is on the box anyway (CLAUDE.md stack). Runs at the lowest CPU priority.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

TARGET_LUFS = -16.0
MAX_GAIN_DB = 12.0
SILENCE_LUFS = -70.0  # ebur128 reports this for silence: nothing to correct
TIMEOUT_S = 30 * 60
_INTEGRATED = re.compile(r"^\[ffmpeg\]\s+I:\s+(-?\d+(?:\.\d+)?) LUFS\s*$", re.MULTILINE)


def measure_args(mpv: str, path: Path) -> list[str]:
    return [
        "nice", "-n", "19", mpv, "--no-config", "--idle=no", "--no-video",
        "--ao=null", "--ao-null-untimed", "--af=lavfi=[ebur128=framelog=quiet]",
        "--msg-level=all=error,ffmpeg=v", "--", str(path),
    ]  # fmt: skip


def integrated_loudness(output: str) -> float | None:
    """The last integrated loudness in the log (the summary at the end of the file)."""
    values = _INTEGRATED.findall(output)
    return float(values[-1]) if values else None


def gain_for(lufs: float) -> float | None:
    """dB to reach the target, limited to ±12 dB; None for silence (SPEC v0.8 §8.2)."""
    if lufs <= SILENCE_LUFS:
        return None
    return round(max(-MAX_GAIN_DB, min(MAX_GAIN_DB, TARGET_LUFS - lufs)), 1)


class MpvLoudness:
    def __init__(self, mpv: str = "mpv") -> None:
        self.mpv = mpv

    async def measure(self, path: Path) -> float | None:
        """Integrated loudness in LUFS, or None if mpv could not read the file."""
        try:
            proc = await asyncio.create_subprocess_exec(
                *measure_args(self.mpv, path),
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
        except OSError:
            log.warning("mpv not available for loudness measurement")
            return None
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), TIMEOUT_S)
        except (TimeoutError, asyncio.CancelledError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()
            await proc.wait()
            raise
        return integrated_loudness(out.decode("utf-8", "replace"))
