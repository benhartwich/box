"""SPEC §9.4: button map, repeats and 5-second combinations."""

from __future__ import annotations

from myboxi_agent.core.buttons import ButtonTracker
from myboxi_agent.core.clock import FakeClock
from myboxi_agent.core.model import Action


def test_volume_acts_on_press_and_repeats_while_held() -> None:
    clock = FakeClock()
    t = ButtonTracker(clock)
    assert t.press("volume_up") == [Action.VOLUME_UP]
    clock.advance(0.4)
    assert t.tick() == []
    clock.advance(0.1)
    assert t.tick() == [Action.VOLUME_UP]
    clock.advance(0.25)
    assert t.tick() == [Action.VOLUME_UP]
    assert t.release("volume_up") == []


def test_play_pause_and_next_act_on_release() -> None:
    t = ButtonTracker(FakeClock())
    assert t.press("play_pause") == []
    assert t.release("play_pause") == [Action.PLAY_PAUSE]
    assert t.press("next") == []
    assert t.release("next") == [Action.NEXT]


def test_setup_combination_after_five_seconds_once() -> None:
    clock = FakeClock()
    t = ButtonTracker(clock)
    t.press("volume_up")
    clock.advance(0.1)
    assert t.press("volume_down") == []  # second key of the combo: no volume change
    clock.advance(4.9)
    assert Action.SETUP_MODE not in t.tick()
    clock.advance(0.2)
    assert t.tick() == [Action.SETUP_MODE]
    clock.advance(3)
    assert t.tick() == []  # fired once; no repeats while in a combo
    assert t.release("volume_up") == []
    assert t.release("volume_down") == []


def test_repair_combination_suppresses_single_actions() -> None:
    clock = FakeClock()
    t = ButtonTracker(clock)
    t.press("play_pause")
    t.press("next")
    clock.advance(5)
    assert t.tick() == [Action.REPAIR]
    assert t.release("next") == []
    assert t.release("play_pause") == []


def test_short_combo_press_does_nothing() -> None:
    clock = FakeClock()
    t = ButtonTracker(clock)
    t.press("play_pause")
    t.press("next")
    clock.advance(1)
    assert t.tick() == []
    assert t.release("play_pause") == []
    assert t.release("next") == []
