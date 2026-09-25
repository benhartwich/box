"""The only place that computes an effective volume (CLAUDE.md rule 4, SPEC §9.2, §5.6)."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from myboxi_protocol.state import DeviceConfig, QuietHours

STEP = 5


@dataclass(frozen=True)
class VolumeLimits:
    ceiling: int
    locked: bool  # quiet hours with ``lock``: no playback at all


def _minutes(hhmm: str) -> int:
    h, m = hhmm.split(":")
    return int(h) * 60 + int(m)


def in_quiet_hours(quiet: QuietHours, local: dt.datetime) -> bool:
    start, end, now = _minutes(quiet.start), _minutes(quiet.end), local.hour * 60 + local.minute
    if start == end:
        return False
    if start < end:
        return start <= now < end
    return now >= start or now < end  # wraps past midnight, e.g. 19:30-06:30


def _local(now: dt.datetime, timezone: str) -> dt.datetime:
    try:
        return now.astimezone(ZoneInfo(timezone))
    except (ZoneInfoNotFoundError, ValueError):
        return now.astimezone(ZoneInfo("Europe/Vienna"))


def limits(config: DeviceConfig, now: dt.datetime, time_trusted: bool) -> VolumeLimits:
    ceiling = config.max_volume
    quiet = config.quiet_hours
    if quiet is None:
        return VolumeLimits(ceiling, locked=False)
    if not time_trusted:
        # SPEC §5.6: without trusted time the quiet-hour cap applies all the time. A lock
        # cannot apply all the time (the box would stay silent after every offline boot), so
        # it degrades to max_volume (SPEC v0.5 §5.6).
        if quiet.max_volume is not None:
            ceiling = min(ceiling, quiet.max_volume)
        return VolumeLimits(ceiling, locked=False)
    if in_quiet_hours(quiet, _local(now, config.timezone)):
        if quiet.lock:
            return VolumeLimits(0, locked=True)
        if quiet.max_volume is not None:
            ceiling = min(ceiling, quiet.max_volume)
    return VolumeLimits(ceiling, locked=False)


def effective(requested: int, config: DeviceConfig, now: dt.datetime, time_trusted: bool) -> int:
    """effective = min(requested, max_volume, quiet-hour limit if active) (SPEC §9.2)."""
    lim = limits(config, now, time_trusted)
    if lim.locked:
        return 0
    return max(0, min(requested, lim.ceiling, 100))


PROMPT_MINIMUM = 20


def prompt_volume(
    requested: int, config: DeviceConfig, now: dt.datetime, time_trusted: bool
) -> int:
    """Prompts must be audible (SPEC §1.7) even at volume 0 or during a quiet-hour lock, but
    never louder than the ceiling (max_volume and the quiet-hour cap)."""
    lim = limits(config, now, time_trusted)
    ceiling = min(config.max_volume, lim.ceiling) if not lim.locked else config.max_volume
    if lim.locked and config.quiet_hours and config.quiet_hours.max_volume is not None:
        ceiling = min(ceiling, config.quiet_hours.max_volume)
    wanted = max(effective(requested, config, now, time_trusted), PROMPT_MINIMUM)
    return max(0, min(wanted, ceiling))
