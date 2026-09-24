"""The set of adapters an agent runs with: simulated or real hardware."""

from __future__ import annotations

from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from myboxi_agent.adapters.base import Buttons, Reader
from myboxi_agent.core.clock import Clock
from myboxi_agent.core.ports import Announcer, Player, System

VolumeSource = Callable[[], int]
Background = Callable[[], Coroutine[Any, Any, None]]


@dataclass
class Adapters:
    clock: Clock
    reader: Reader
    buttons: Buttons
    player: Player
    system: System
    # The announcer's volume comes from the controller (core.volume.prompt_volume), which is
    # created after the adapters; hence a factory.
    announcer_factory: Callable[[VolumeSource], Announcer]
    background: list[Background] = field(default_factory=list[Background])


def sim_adapters() -> Adapters:
    from myboxi_agent.adapters.clock import SystemClock
    from myboxi_agent.adapters.sim import (
        SimAnnouncer,
        SimButtons,
        SimPlayer,
        SimReader,
        SimSystem,
    )

    return Adapters(
        clock=SystemClock(assume_trusted=True),
        reader=SimReader(),
        buttons=SimButtons(),
        player=SimPlayer(),
        system=SimSystem(),
        announcer_factory=lambda _volume: SimAnnouncer(),
    )
