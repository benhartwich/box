"""Setup phase after pairing and the button test (SPEC v0.6 §9.6)."""

from __future__ import annotations

import asyncio
import datetime as dt
import random
import uuid
from pathlib import Path

from myboxi_agent.adapters.bundle import sim_adapters
from myboxi_agent.adapters.sim import SimAnnouncer, SimButtons
from myboxi_agent.app import App
from myboxi_agent.config import Settings
from myboxi_agent.core.clock import FakeClock
from myboxi_agent.core.controller import Controller
from myboxi_agent.core.model import Prompt
from myboxi_agent.core.setup_phase import SETUP_PHASE_S, SetupPhase
from myboxi_agent.testing import (
    FakeAnnouncer,
    FakeLibrary,
    FakeOutbox,
    FakePlayer,
    FakeResumeStore,
    FakeSystem,
    playable,
)
from myboxi_protocol.state import DeviceConfig

T0 = dt.datetime(2026, 9, 25, 10, 0, tzinfo=dt.UTC)


def test_phase_needs_pairing() -> None:
    phase = SetupPhase(FakeClock(T0), lambda: None)
    phase.started()
    assert not phase.active()
    assert not phase.record("next")


def test_phase_lasts_an_hour_after_pairing() -> None:
    clock = FakeClock(T0)
    phase = SetupPhase(clock, lambda: T0)
    phase.started()
    assert phase.active()
    clock.advance(SETUP_PHASE_S - 1)
    assert phase.active()
    clock.advance(2)
    assert not phase.active()


def test_phase_survives_a_reboot_only_with_trusted_time() -> None:
    clock = FakeClock(T0 + dt.timedelta(minutes=20), trusted=False)
    phase = SetupPhase(clock, lambda: T0)  # new boot: no started()
    assert not phase.active()
    clock.trusted = True
    assert phase.active()
    clock.set_wall(T0 + dt.timedelta(minutes=61))
    assert not phase.active()


def test_new_pairing_restarts_the_button_test() -> None:
    phase = SetupPhase(FakeClock(T0), lambda: T0)
    phase.started()
    assert phase.record("next")
    phase.started()
    assert phase.seen == set()


def _controller(phase: SetupPhase) -> tuple[Controller, FakeAnnouncer, FakeLibrary]:
    announcer, library = FakeAnnouncer(), FakeLibrary()
    ctl = Controller(
        clock=phase.clock,
        player=FakePlayer(),
        announcer=announcer,
        outbox=FakeOutbox(),
        resume_store=FakeResumeStore(),
        library=library,
        system=FakeSystem(),
        config=DeviceConfig,
        rng=random.Random(1),
        setup_phase=phase,
    )
    return ctl, announcer, library


def test_button_tone_only_in_the_phase_and_when_silent() -> None:
    clock = FakeClock(T0)
    paired: list[dt.datetime | None] = [None]
    phase = SetupPhase(clock, lambda: paired[0])
    ctl, announcer, library = _controller(phase)

    ctl.button_seen("next")  # not paired: normal box, no beeps
    assert announcer.said == []

    paired[0] = T0
    phase.started()
    ctl.button_seen("volume_up")
    assert announcer.said == [(Prompt.TONE_BUTTON,)]
    assert phase.seen == {"volume_up"}

    library.by_uid["04112233"] = playable()
    ctl.token_placed("04112233")
    announcer.said.clear()
    ctl.button_seen("play_pause")  # while playing: counted, but no beep over the story
    assert announcer.said == []
    assert phase.seen == {"volume_up", "play_pause"}

    clock.advance(SETUP_PHASE_S)
    ctl.token_removed()
    ctl.button_seen("next")
    assert "next" not in phase.seen


async def test_button_test_in_reported_and_beeps(tmp_path: Path) -> None:
    adapters = sim_adapters()
    app = App(Settings(data_dir=tmp_path, sim=True), adapters)
    assert app.reported_data().button_test is None

    app.state.set_paired(uuid.uuid4(), "s" * 43)
    app.setup_phase.started()
    assert app.sync.in_setup_phase()
    task = asyncio.create_task(app.run())
    try:
        assert isinstance(adapters.buttons, SimButtons)
        await adapters.buttons.hold(["volume_up", "volume_down"], 0.05)
        await adapters.buttons.hold(["next"], 0.05)
        for _ in range(50):
            data = app.reported_data().button_test
            if data is not None and len(data.seen) == 3:
                break
            await asyncio.sleep(0.02)
        data = app.reported_data().button_test
        assert data is not None
        assert data.seen == ["next", "volume_down", "volume_up"]
        assert app.status()["setup_phase"] is True
        announcer = app.announcer
        assert isinstance(announcer, SimAnnouncer)
        assert [h for h in announcer.history if h == (Prompt.TONE_BUTTON,)] != []
    finally:
        app.stop()
        await task
