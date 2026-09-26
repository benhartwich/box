"""Start points from the app (SPEC v0.11 §3.9) and radio streams (§8.3)."""

from __future__ import annotations

import datetime as dt
import random
import uuid
from pathlib import Path
from typing import Any

import pytest

from myboxi_agent.core.clock import FakeClock
from myboxi_agent.core.controller import Controller
from myboxi_agent.core.model import Action, PlanItem, Playable, Prompt, ResumePoint, Unavailable
from myboxi_agent.store.db import connect
from myboxi_agent.store.repos import AssetRepo, Database, LibraryRepo, StateRepo, asset_path
from myboxi_agent.testing import (
    FakeAnnouncer,
    FakeLibrary,
    FakeOutbox,
    FakePlayer,
    FakeResumeStore,
    FakeSystem,
)
from myboxi_protocol.events import PlaybackErrorData
from myboxi_protocol.state import DeviceConfig, StateResponse

UID = "04A2B3C4D5E680"
RADIO = "https://stream.example.org/kinderradio.mp3"


@pytest.fixture
def db(tmp_path: Path) -> Database:
    return Database(connect(tmp_path / "myboxi.db"), tmp_path / "assets", FakeClock())


def snapshot(
    *,
    kind: str = "collection",
    start_at: dict[str, Any] | None = None,
    token_id: uuid.UUID | None = None,
    content_id: uuid.UUID | None = None,
    resume: bool = True,
    config_rev: int = 2,
) -> StateResponse:
    token_id, content_id = token_id or uuid.uuid4(), content_id or uuid.uuid4()
    source = {"collection": {}, "stream": {"url": RADIO}}[kind]
    binding: dict[str, Any] = {"token_id": str(token_id), "content_id": str(content_id),
                               "resume": resume}  # fmt: skip
    if start_at is not None:
        binding["start_at"] = start_at
    items = [
        {"content_id": str(content_id), "position": i, "asset_sha256": f"{i:064x}",
         "bytes": 10, "title": f"Kapitel {i + 1}", "duration_ms": 1000}
        for i in range(4)
    ] if kind == "collection" else []  # fmt: skip
    return StateResponse.model_validate(
        {
            "full": True, "config_rev": config_rev, "device_rev": 1,
            "upserts": {
                "token": [{"id": str(token_id), "uid": UID, "label": "Hase"}],
                "content": [{"id": str(content_id), "kind": kind, "title": "T", "rev": 1,
                             "source": source}],
                "content_item": items,
                "binding": [binding],
            },
            "device_config": {},
        }
    )  # fmt: skip


def put_assets(db: Database) -> None:
    for i in range(4):
        sha = f"{i:064x}"
        path = asset_path(db.asset_dir, sha)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x")
        AssetRepo(db).register(sha, 1)


def test_start_point_applies_once(db: Database) -> None:
    """SPEC v0.11 §3.9: at the next placement only; afterwards the resume position again."""
    put_assets(db)
    lib = LibraryRepo(db)
    start_id = str(uuid.uuid4())
    snap = snapshot(start_at={"id": start_id, "item_index": 2})
    lib.activate(snap)
    first = lib.resolve(UID)
    assert isinstance(first, Playable)
    assert first.start == ResumePoint(2, 0)
    second = lib.resolve(UID)
    assert isinstance(second, Playable)
    assert second.start is None
    # the same start point again after a sync changes nothing; a new one applies again
    lib.activate(snap)
    third = lib.resolve(UID)
    assert isinstance(third, Playable)
    assert third.start is None
    token = snap.upserts.token[0].id
    content = snap.upserts.content[0].id
    lib.activate(snapshot(start_at={"id": str(uuid.uuid4()), "item_index": 0},
                          token_id=token, content_id=content, config_rev=3))  # fmt: skip
    fourth = lib.resolve(UID)
    assert isinstance(fourth, Playable)
    assert fourth.start == ResumePoint(0, 0)


def test_start_point_behind_the_last_title_starts_at_the_first(db: Database) -> None:
    put_assets(db)
    lib = LibraryRepo(db)
    lib.activate(snapshot(start_at={"id": str(uuid.uuid4()), "item_index": 9}))
    plan = lib.resolve(UID)
    assert isinstance(plan, Playable)
    assert plan.start == ResumePoint(0, 0)


def test_radio_resolves_to_a_live_stream(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(snapshot(kind="stream", start_at={"id": str(uuid.uuid4()), "item_index": 1}))
    plan = lib.resolve(UID)
    assert isinstance(plan, Playable)
    assert (plan.provider, plan.items[0].source, plan.resume, plan.start) == (
        "stream", RADIO, False, None,
    )  # fmt: skip


def test_radio_can_be_switched_off(db: Database) -> None:
    lib = LibraryRepo(db)
    lib.activate(snapshot(kind="stream"))
    StateRepo(db).apply_device_config(DeviceConfig(providers_enabled=["local"]), 2)
    res = lib.resolve(UID)
    assert isinstance(res, Unavailable)
    assert (res.provider, res.code) == ("stream", "disabled")


# --- controller ----------------------------------------------------------------------------


def controller(plan: Playable, resume: FakeResumeStore | None = None) -> tuple[Controller, Any]:
    player, announcer, outbox = FakePlayer(), FakeAnnouncer(), FakeOutbox()
    ctl = Controller(
        clock=FakeClock(dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.UTC)),
        player=player, announcer=announcer, outbox=outbox,
        resume_store=resume or FakeResumeStore(), library=FakeLibrary({UID: plan}),
        system=FakeSystem(), config=DeviceConfig, rng=random.Random(1),
    )  # fmt: skip
    return ctl, (player, announcer, outbox)


def chapters(**kw: Any) -> Playable:
    items = tuple(PlanItem(f"/a/{i}.opus", f"Kapitel {i + 1}", 60_000) for i in range(4))
    return Playable(uuid.uuid4(), uuid.uuid4(), items, True, False, "off", **kw)


def test_start_point_wins_over_the_resume_position() -> None:
    plan = chapters(start=ResumePoint(2, 0))
    resume = FakeResumeStore({plan.token_id: ResumePoint(1, 30_000)})
    ctl, (player, _, _) = controller(plan, resume)
    ctl.token_placed(UID)
    assert player.calls == ["play:2@0"]


def test_start_point_with_shuffle_starts_at_the_chosen_title() -> None:
    plan = chapters(start=ResumePoint(3, 0))
    plan = Playable(plan.token_id, plan.content_id, plan.items, True, True, "off",
                    start=ResumePoint(3, 0))  # fmt: skip
    ctl, (player, _, _) = controller(plan)
    ctl.token_placed(UID)
    assert player.sources[player.index] == "/a/3.opus"


def radio() -> Playable:
    return Playable(uuid.uuid4(), uuid.uuid4(), (PlanItem(RADIO, RADIO, 0),), False, False,
                    "off", "stream")  # fmt: skip


def test_radio_next_has_no_next_title() -> None:
    ctl, (player, announcer, _) = controller(radio())
    ctl.token_placed(UID)
    ctl.button(Action.NEXT)
    assert announcer.said[-1] == (Prompt.TONE_ERROR,)
    assert player.state == "playing"


def test_radio_offline_says_so() -> None:
    plan = radio()
    ctl, (_, announcer, outbox) = controller(plan)
    ctl.token_placed(UID)
    ctl.player_error("decode_error")
    assert announcer.said[-1] == (Prompt.UNAVAILABLE, Prompt.TONE_ERROR)
    assert outbox.of("playback_error") == [
        PlaybackErrorData(token_id=plan.token_id, provider="stream", code="stream_error")
    ]
