"""SPEC §9.1 figure logic, §9.2 volume, §9.5 pairing, CLAUDE.md rule 5 (resume)."""

from __future__ import annotations

import datetime as dt
import random
import uuid
from dataclasses import dataclass
from typing import Any

import pytest

from myboxi_agent.core.clock import FakeClock
from myboxi_agent.core.controller import RESUME_SAVE_EVERY_S, Controller
from myboxi_agent.core.model import (
    Action,
    Loading,
    PlanItem,
    Playable,
    Prompt,
    ResumePoint,
    Unavailable,
)
from myboxi_agent.testing import (
    FakeAnnouncer,
    FakeLibrary,
    FakeOutbox,
    FakePlayer,
    FakeResumeStore,
    FakeSystem,
    playable,
)
from myboxi_protocol.events import (
    PlaybackErrorData,
    ResumePositionData,
    TokenPlayedData,
    TokenUnknownData,
)
from myboxi_protocol.state import DeviceConfig

UID = "04A2B3C4D5E680"


@dataclass
class Box:
    clock: FakeClock
    player: FakePlayer
    announcer: FakeAnnouncer
    outbox: FakeOutbox
    resume: FakeResumeStore
    library: FakeLibrary
    system: FakeSystem
    cfg: dict[str, Any]
    ctl: Controller

    def set_config(self, **kw: Any) -> None:
        self.cfg.update(kw)


@pytest.fixture
def box() -> Box:
    clock = FakeClock(dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.UTC))  # 12:00 Vienna
    player, announcer, outbox = FakePlayer(), FakeAnnouncer(), FakeOutbox()
    resume, library, system = FakeResumeStore(), FakeLibrary(), FakeSystem()
    cfg: dict[str, Any] = {"max_volume": 55, "start_volume": 35}
    ctl = Controller(
        clock=clock,
        player=player,
        announcer=announcer,
        outbox=outbox,
        resume_store=resume,
        library=library,
        system=system,
        config=lambda: DeviceConfig.model_validate(cfg),
        rng=random.Random(4),
    )
    return Box(clock, player, announcer, outbox, resume, library, system, cfg, ctl)


# --- §9.1 ---------------------------------------------------------------------------------


def test_known_figure_plays_from_resume_position_at_start_volume(box: Box) -> None:
    plan = playable()
    box.library.by_uid[UID] = plan
    box.resume.points[plan.token_id] = ResumePoint(1, 42_000)
    box.ctl.token_placed(UID)
    assert box.announcer.said[0] == (Prompt.TONE_START,)
    assert box.player.calls == ["play:1@42000"]
    assert box.player.volume == 35
    played = box.outbox.of("token_played")
    assert played == [TokenPlayedData(token_id=plan.token_id, content_id=plan.content_id)]
    assert box.ctl.status().status == "playing"


def test_unknown_figure_announces_and_reports(box: Box) -> None:
    box.ctl.token_placed(UID)
    assert box.announcer.said == [(Prompt.UNKNOWN_TOKEN,)]
    assert box.outbox.of("token_unknown") == [TokenUnknownData(uid=UID)]
    assert box.player.calls == []


def test_removal_pauses_and_saves_position(box: Box) -> None:
    plan = playable()
    box.library.by_uid[UID] = plan
    box.ctl.token_placed(UID)
    box.player.index, box.player.position_ms = 2, 7_000
    box.ctl.token_removed()
    assert box.player.state == "paused"
    assert box.resume.points[plan.token_id] == ResumePoint(2, 7_000)
    assert box.outbox.of("resume_position")[-1] == ResumePositionData(
        token_id=plan.token_id, item_index=2, position_ms=7_000
    )


def test_removal_with_continue_keeps_playing(box: Box) -> None:
    box.set_config(on_token_removed="continue")
    box.library.by_uid[UID] = playable()
    box.ctl.token_placed(UID)
    box.ctl.token_removed()
    assert box.player.state == "playing"


def test_replacing_the_figure_resumes(box: Box) -> None:
    plan = playable()
    box.library.by_uid[UID] = plan
    box.ctl.token_placed(UID)
    box.player.index, box.player.position_ms = 1, 30_000
    box.ctl.token_removed()
    box.ctl.token_placed(UID)
    assert box.player.calls[-1] == "play:1@30000"


def test_without_resume_starts_at_the_beginning(box: Box) -> None:
    plan = playable(resume=False)
    box.library.by_uid[UID] = plan
    box.resume.points[plan.token_id] = ResumePoint(2, 5_000)
    box.ctl.token_placed(UID)
    assert box.player.calls == ["play:0@0"]


def test_content_end_is_silence_and_position_back_to_start(box: Box) -> None:
    plan = playable()
    box.library.by_uid[UID] = plan
    box.ctl.token_placed(UID)
    box.ctl.playlist_finished()
    assert box.player.state == "stopped"
    assert box.resume.points[plan.token_id] == ResumePoint(0, 0)
    assert box.ctl.status().status == "stopped"


@pytest.mark.parametrize("repeat", ["all", "one"])
def test_repeat_is_passed_to_the_player(box: Box, repeat: str) -> None:
    box.library.by_uid[UID] = playable(repeat=repeat)  # pyright: ignore[reportArgumentType]
    box.ctl.token_placed(UID)
    assert box.player.repeat == repeat


def test_shuffle_starts_with_the_resume_item(box: Box) -> None:
    plan = playable(n=6, shuffle=True)
    box.library.by_uid[UID] = plan
    box.resume.points[plan.token_id] = ResumePoint(4, 1_000)
    box.ctl.token_placed(UID)
    assert box.player.calls == ["play:0@1000"]
    assert box.player.sources[0] == "/a/4.opus"
    assert sorted(box.player.sources) == sorted(i.source for i in plan.items)


def test_unavailable_provider_announces_error_and_reports(box: Box) -> None:
    plan = playable()
    box.library.by_uid[UID] = Unavailable(
        plan.token_id, plan.content_id, "spotify", "not_configured"
    )
    box.ctl.token_placed(UID)
    assert box.announcer.said == [(Prompt.UNAVAILABLE, Prompt.TONE_ERROR)]
    assert box.outbox.of("playback_error") == [
        PlaybackErrorData(token_id=plan.token_id, provider="spotify", code="not_configured")
    ]


def test_loading_binding_announces_loading(box: Box) -> None:
    """SPEC §5.3: new binding still downloading, no previous binding."""
    box.library.by_uid[UID] = Loading(playable().token_id)
    box.ctl.token_placed(UID)
    assert box.announcer.said == [(Prompt.LOADING,)]


def test_other_figure_saves_the_previous_session(box: Box) -> None:
    first, second = playable(), playable()
    box.library.by_uid[UID] = first
    box.library.by_uid["04FFFFFFFF"] = second
    box.ctl.token_placed(UID)
    box.player.index, box.player.position_ms = 1, 9_000
    box.ctl.token_placed("04FFFFFFFF")
    assert box.resume.points[first.token_id] == ResumePoint(1, 9_000)
    assert box.ctl.status().token_id == second.token_id


def test_player_error_is_audible_and_reported(box: Box) -> None:
    box.library.by_uid[UID] = playable()
    box.ctl.token_placed(UID)
    box.ctl.player_error("decode")
    assert box.announcer.said[-1] == (Prompt.TONE_ERROR,)
    assert len(box.outbox.of("playback_error")) == 1


# --- buttons (§9.4) -----------------------------------------------------------------------


def test_play_pause_toggles(box: Box) -> None:
    box.library.by_uid[UID] = playable()
    box.ctl.token_placed(UID)
    box.ctl.button(Action.PLAY_PAUSE)
    assert box.player.state == "paused"
    box.ctl.button(Action.PLAY_PAUSE)
    assert box.player.state == "playing"


def test_play_pause_without_anything_is_audible(box: Box) -> None:
    box.ctl.button(Action.PLAY_PAUSE)
    assert box.announcer.said == [(Prompt.TONE_ERROR,)]


def test_next_and_end_of_list(box: Box) -> None:
    box.library.by_uid[UID] = playable(n=2)
    box.ctl.token_placed(UID)
    box.ctl.button(Action.NEXT)
    assert box.player.calls[-1] == "play:1@0"
    box.player.index = 1
    box.ctl.button(Action.NEXT)
    assert box.player.state == "stopped"


def test_next_wraps_with_repeat_all(box: Box) -> None:
    box.library.by_uid[UID] = playable(n=2, repeat="all")
    box.ctl.token_placed(UID)
    box.player.index = 1
    box.ctl.button(Action.NEXT)
    assert box.player.calls[-1] == "play:0@0"


def test_setup_and_repair_combinations(box: Box) -> None:
    box.ctl.button(Action.SETUP_MODE)
    box.ctl.button(Action.REPAIR)
    assert (box.system.setup_requests, box.system.repair_requests) == (1, 1)
    assert (Prompt.REPAIR,) in box.announcer.said


# --- §9.2 volume --------------------------------------------------------------------------


def test_buttons_never_exceed_max_volume_nor_build_headroom(box: Box) -> None:
    box.library.by_uid[UID] = playable()
    box.ctl.token_placed(UID)
    for _ in range(10):
        box.ctl.button(Action.VOLUME_UP)
    assert box.player.volume == 55
    box.ctl.button(Action.VOLUME_DOWN)
    assert box.player.volume == 50  # one step down from the ceiling, not from 85


def test_quiet_hours_lower_volume_when_they_begin(box: Box) -> None:
    box.set_config(quiet_hours={"start": "19:30", "end": "06:30", "max_volume": 20})
    box.library.by_uid[UID] = playable()
    box.ctl.token_placed(UID)
    assert box.player.volume == 35
    box.clock.set_wall(dt.datetime(2026, 9, 24, 17, 31, tzinfo=dt.UTC))  # 19:31 Vienna
    box.ctl.tick()
    assert box.player.volume == 20


def test_quiet_lock_prevents_and_stops_playback(box: Box) -> None:
    box.set_config(quiet_hours={"start": "19:30", "end": "06:30", "lock": True})
    box.library.by_uid[UID] = playable()
    box.ctl.token_placed(UID)
    assert box.player.state == "playing"
    box.clock.set_wall(dt.datetime(2026, 9, 24, 17, 31, tzinfo=dt.UTC))
    box.ctl.tick()
    assert box.player.state == "paused"
    assert box.announcer.said[-1] == (Prompt.QUIET_TIME,)
    box.ctl.token_removed()
    box.ctl.token_placed(UID)
    assert box.player.state == "paused"
    assert box.announcer.said[-1] == (Prompt.QUIET_TIME,)


def test_untrusted_time_caps_volume_all_day(box: Box) -> None:
    box.set_config(quiet_hours={"start": "19:30", "end": "06:30", "max_volume": 20})
    box.clock.trusted = False
    box.library.by_uid[UID] = playable()
    box.ctl.token_placed(UID)
    assert box.player.volume == 20


# --- resume (CLAUDE.md rule 5) and sleep timer ---------------------------------------------


def test_position_is_saved_at_least_every_ten_seconds(box: Box) -> None:
    plan = playable()
    box.library.by_uid[UID] = plan
    box.ctl.token_placed(UID)
    box.player.position_ms = 9_000
    box.clock.advance(RESUME_SAVE_EVERY_S - 1)
    box.ctl.tick()
    assert plan.token_id not in box.resume.points
    box.clock.advance(1)
    box.ctl.tick()
    assert box.resume.points[plan.token_id] == ResumePoint(0, 9_000)


def test_sleep_timer_pauses(box: Box) -> None:
    box.set_config(sleep_timer_min=20)
    box.library.by_uid[UID] = playable()
    box.ctl.token_placed(UID)
    box.clock.advance(20 * 60)
    box.ctl.tick()
    assert box.player.state == "paused"


# --- pairing (§9.5) -----------------------------------------------------------------------


def test_pairing_code_is_announced_repeated_and_confirmed(box: Box) -> None:
    box.ctl.pairing_started("471193")
    expected = (Prompt.TONE_ATTENTION, Prompt.PAIRING_INTRO, *(f"digit_{d}" for d in "471193"))
    assert box.announcer.said == [expected]
    box.clock.advance(29)
    box.ctl.tick()
    assert len(box.announcer.said) == 1
    box.clock.advance(1)
    box.ctl.tick()
    assert len(box.announcer.said) == 2
    box.ctl.button(Action.PLAY_PAUSE)
    assert len(box.announcer.said) == 3
    box.ctl.pairing_finished(success=True)
    assert box.announcer.said[-1] == (Prompt.PAIRING_DONE,)
    box.clock.advance(60)
    box.ctl.tick()
    assert box.announcer.said[-1] == (Prompt.PAIRING_DONE,)


# --- SPEC v0.8 §8.2, §3.10: podcasts ------------------------------------------------------


def _episodes(*keys: str) -> tuple[PlanItem, ...]:
    return tuple(PlanItem(f"/p/{k}.mp3", k, 60_000, key=k) for k in keys)


def test_podcast_resumes_the_same_episode_after_a_new_one_arrived(box: Box) -> None:
    token = uuid.uuid4()
    before = Playable(token, uuid.uuid4(), _episodes("e2", "e1"), True, False, "off", "podcast")
    box.library.by_uid[UID] = before
    box.ctl.token_placed(UID)
    box.player.index, box.player.position_ms = 1, 30_000  # listening to e1
    box.ctl.token_removed()
    assert box.resume.points[token] == ResumePoint(1, 30_000, "e1")
    assert box.outbox.of("resume_position")[-1] == ResumePositionData(
        token_id=token, item_index=1, position_ms=30_000, item_key="e1"
    )
    # a new episode e3 shifts the list: the box continues e1, now at index 2
    box.library.by_uid[UID] = Playable(
        token, before.content_id, _episodes("e3", "e2", "e1"), True, False, "off", "podcast"
    )
    box.ctl.token_placed(UID)
    assert box.player.calls[-1] == "play:2@30000"


def test_podcast_starts_over_when_the_saved_episode_is_gone(box: Box) -> None:
    token = uuid.uuid4()
    box.resume.points[token] = ResumePoint(0, 30_000, "old")
    box.library.by_uid[UID] = Playable(
        token, uuid.uuid4(), _episodes("new"), True, False, "off", "podcast"
    )
    box.ctl.token_placed(UID)
    assert box.player.calls == ["play:0@0"]


def test_player_error_reports_the_real_provider(box: Box) -> None:
    token = uuid.uuid4()
    box.library.by_uid[UID] = Playable(
        token, uuid.uuid4(), _episodes("e1"), True, False, "off", "podcast"
    )
    box.ctl.token_placed(UID)
    box.ctl.player_error("decode_error")
    assert box.outbox.of("playback_error") == [
        PlaybackErrorData(token_id=token, provider="podcast", code="decode_error")
    ]
