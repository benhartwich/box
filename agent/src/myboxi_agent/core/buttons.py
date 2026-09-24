"""Button presses to actions, incl. repeats and 5-second combinations (SPEC §9.4)."""

from __future__ import annotations

from dataclasses import dataclass, field

from myboxi_agent.core.clock import Clock
from myboxi_agent.core.model import Action

ButtonName = str
REPEAT_DELAY_S = 0.5
REPEAT_EVERY_S = 0.25
COMBO_HOLD_S = 5.0

_VOLUME = {"volume_up": Action.VOLUME_UP, "volume_down": Action.VOLUME_DOWN}
_ON_RELEASE = {"play_pause": Action.PLAY_PAUSE, "next": Action.NEXT}
_COMBOS: dict[frozenset[str], Action] = {
    frozenset({"volume_up", "volume_down"}): Action.SETUP_MODE,
    frozenset({"play_pause", "next"}): Action.REPAIR,
}


@dataclass
class _Held:
    since: float
    next_repeat: float
    in_combo: bool = False


@dataclass
class ButtonTracker:
    clock: Clock
    _held: dict[ButtonName, _Held] = field(default_factory=dict[ButtonName, _Held])
    _fired_combos: set[frozenset[str]] = field(default_factory=set[frozenset[str]])

    def press(self, button: ButtonName) -> list[Action]:
        now = self.clock.monotonic()
        self._held[button] = _Held(since=now, next_repeat=now + REPEAT_DELAY_S)
        for combo in _COMBOS:
            if button in combo and combo <= self._held.keys():
                for b in combo:
                    self._held[b].in_combo = True
        if button in _VOLUME and not self._held[button].in_combo:
            return [_VOLUME[button]]
        return []

    def release(self, button: ButtonName) -> list[Action]:
        held = self._held.pop(button, None)
        self._fired_combos = {c for c in self._fired_combos if c <= self._held.keys()}
        if held is None or held.in_combo:
            return []
        if button in _ON_RELEASE:
            return [_ON_RELEASE[button]]
        return []

    def tick(self) -> list[Action]:
        now = self.clock.monotonic()
        actions: list[Action] = []
        for combo, action in _COMBOS.items():
            if combo <= self._held.keys() and combo not in self._fired_combos:
                started = max(self._held[b].since for b in combo)
                if now - started >= COMBO_HOLD_S:
                    self._fired_combos.add(combo)
                    actions.append(action)
        for button, held in self._held.items():
            if button in _VOLUME and not held.in_combo and now >= held.next_repeat:
                held.next_repeat = now + REPEAT_EVERY_S
                actions.append(_VOLUME[button])
        return actions
