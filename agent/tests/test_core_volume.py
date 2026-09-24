"""SPEC §9.2 (single volume policy) and §5.6 (quiet hours without trusted time)."""

from __future__ import annotations

import datetime as dt

import pytest

from myboxi_agent.core import volume
from myboxi_protocol.state import DeviceConfig, QuietHours

VIENNA_NOON_UTC = dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.UTC)  # 12:00 in Vienna (CEST)
VIENNA_2030_UTC = dt.datetime(2026, 9, 24, 18, 30, tzinfo=dt.UTC)  # 20:30 in Vienna
VIENNA_0500_UTC = dt.datetime(2026, 9, 25, 3, 0, tzinfo=dt.UTC)  # 05:00 in Vienna


def cfg(**kw: object) -> DeviceConfig:
    return DeviceConfig.model_validate({"max_volume": 55, **kw})


def test_effective_is_min_of_requested_and_max() -> None:
    assert volume.effective(80, cfg(), VIENNA_NOON_UTC, True) == 55
    assert volume.effective(30, cfg(), VIENNA_NOON_UTC, True) == 30
    assert volume.effective(-5, cfg(), VIENNA_NOON_UTC, True) == 0


@pytest.mark.parametrize(
    ("now", "expected"),
    [(VIENNA_NOON_UTC, 55), (VIENNA_2030_UTC, 20), (VIENNA_0500_UTC, 20)],
)
def test_quiet_hours_cap_in_device_timezone_and_across_midnight(
    now: dt.datetime, expected: int
) -> None:
    c = cfg(quiet_hours={"start": "19:30", "end": "06:30", "max_volume": 20})
    assert volume.effective(80, c, now, True) == expected


def test_untrusted_time_applies_quiet_cap_all_day() -> None:
    """SPEC §5.6: safe fallback min(max_volume, quiet_hours.max_volume) for the whole runtime."""
    c = cfg(quiet_hours={"start": "19:30", "end": "06:30", "max_volume": 20})
    assert volume.effective(80, c, VIENNA_NOON_UTC, False) == 20


def test_lock_blocks_only_with_trusted_time() -> None:
    c = cfg(quiet_hours={"start": "19:30", "end": "06:30", "lock": True})
    assert volume.limits(c, VIENNA_2030_UTC, True).locked
    assert volume.effective(40, c, VIENNA_2030_UTC, True) == 0
    assert not volume.limits(c, VIENNA_NOON_UTC, True).locked
    # Without trusted time a lock cannot be placed in time: fall back to max_volume.
    lim = volume.limits(c, VIENNA_2030_UTC, False)
    assert (lim.locked, lim.ceiling) == (False, 55)


def test_in_quiet_hours_same_day_window() -> None:
    q = QuietHours(start="12:00", end="14:00", max_volume=10)
    assert volume.in_quiet_hours(q, dt.datetime(2026, 1, 1, 13, 59, tzinfo=dt.UTC))
    assert not volume.in_quiet_hours(q, dt.datetime(2026, 1, 1, 14, 0, tzinfo=dt.UTC))
