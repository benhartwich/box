"""Figure logic and playback state machine (SPEC §9.1, §9.2, §9.4, §9.5, §5.3).

All inputs arrive as method calls (reader, buttons, player callbacks, a periodic ``tick``);
all effects go through the ports. Time comes from the injected clock only.
"""

from __future__ import annotations

import random
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Literal

from myboxi_agent.core import volume
from myboxi_agent.core.clock import Clock
from myboxi_agent.core.model import (
    Action,
    Loading,
    Playable,
    Prompt,
    ResumePoint,
    Unavailable,
    Unknown,
    digit_prompt,
)
from myboxi_agent.core.ports import Announcer, Library, Outbox, Player, ResumeStore, System
from myboxi_agent.core.setup_phase import SetupPhase
from myboxi_protocol.events import (
    PlaybackErrorData,
    ResumePositionData,
    TokenPlayedData,
    TokenUnknownData,
)
from myboxi_protocol.state import DeviceConfig

RESUME_SAVE_EVERY_S = 10.0  # CLAUDE.md rule 5
PAIRING_REPEAT_S = 30.0  # SPEC §9.5

PlaybackStatus = Literal["stopped", "playing", "paused"]


@dataclass
class Session:
    plan: Playable
    order: list[int]  # playlist position -> item index
    playing: bool = True
    token_present: bool = True
    play_started: float = 0.0
    last_saved: float = 0.0
    finished: bool = False

    def sources(self) -> list[str]:
        return [self.plan.items[i].source for i in self.order]


@dataclass(frozen=True)
class Status:
    status: PlaybackStatus
    token_id: uuid.UUID | None
    volume: int


@dataclass
class Controller:
    clock: Clock
    player: Player
    announcer: Announcer
    outbox: Outbox
    resume_store: ResumeStore
    library: Library
    system: System
    config: Callable[[], DeviceConfig]
    rng: random.Random = field(default_factory=random.Random)
    setup_phase: SetupPhase | None = None

    requested_volume: int = -1
    session: Session | None = None
    current_uid: str | None = None
    pairing_code: str | None = None
    _applied_volume: int | None = None
    _last_code_announce: float = 0.0

    def __post_init__(self) -> None:
        if self.requested_volume < 0:
            self.requested_volume = self.config().start_volume

    # --- inputs ---------------------------------------------------------------------------

    def token_placed(self, uid: str) -> None:
        self.current_uid = uid
        match self.library.resolve(uid):
            case Unknown():
                self.announcer.announce(Prompt.UNKNOWN_TOKEN)
                self.outbox.emit("token_unknown", TokenUnknownData(uid=uid))
            case Loading():
                self.announcer.announce(Prompt.LOADING)
            case Unavailable() as u:
                self.announcer.announce(Prompt.UNAVAILABLE, Prompt.TONE_ERROR)
                self.outbox.emit(
                    "playback_error",
                    PlaybackErrorData(token_id=u.token_id, provider=u.provider, code=u.code),  # pyright: ignore[reportArgumentType]
                )
            case Playable() as plan:
                self._start(plan)

    def token_removed(self) -> None:
        self.current_uid = None
        s = self.session
        if s is None or not s.token_present:
            return
        s.token_present = False
        if s.playing and self.config().on_token_removed == "pause":
            self._save(emit=True)
            self.player.pause()
            s.playing = False
        else:
            self._save(emit=True)

    def button_seen(self, button: str) -> None:
        """Raw press, before combos and repeats (SPEC v0.6 §9.6): in the setup phase the box
        remembers it for the button test and beeps unless something is playing."""
        if self.setup_phase is None or not self.setup_phase.record(button):
            return
        if self.session is None or not self.session.playing:
            self.announcer.announce(Prompt.TONE_BUTTON)

    def button(self, action: Action) -> None:
        match action:
            case Action.PLAY_PAUSE:
                self._play_pause()
            case Action.VOLUME_UP:
                self._change_volume(+volume.STEP)
            case Action.VOLUME_DOWN:
                self._change_volume(-volume.STEP)
            case Action.NEXT:
                self._next()
            case Action.SETUP_MODE:
                self.system.request_setup_mode()
            case Action.REPAIR:
                self.announcer.announce(Prompt.REPAIR)
                self.system.request_repair()

    def playlist_finished(self) -> None:
        """End of content without repeat: silence, position back to the start (SPEC §9.1)."""
        s = self.session
        if s is None:
            return
        self._finish(s)

    def player_error(self, code: str = "player_error") -> None:
        s = self.session
        self.announcer.announce(Prompt.TONE_ERROR)
        if s is not None:
            self.outbox.emit(
                "playback_error",
                PlaybackErrorData(token_id=s.plan.token_id, provider="local", code=code),
            )
            s.playing = False

    def tick(self) -> None:
        now = self.clock.monotonic()
        cfg = self.config()
        lim = volume.limits(cfg, self.clock.now(), self.clock.time_trusted())
        s = self.session
        if s is not None and s.playing:
            if lim.locked:
                self._pause(s)
                self.announcer.announce(Prompt.QUIET_TIME)
            elif cfg.sleep_timer_min and now - s.play_started >= cfg.sleep_timer_min * 60:
                self._pause(s)
            elif now - s.last_saved >= RESUME_SAVE_EVERY_S:
                self._save(emit=False)
        self._apply_volume()
        if self.pairing_code and now - self._last_code_announce >= PAIRING_REPEAT_S:
            self._announce_code()

    # --- pairing (SPEC §9.5) ----------------------------------------------------------------

    def pairing_started(self, code: str) -> None:
        self.pairing_code = code
        self._announce_code()

    def pairing_finished(self, *, success: bool) -> None:
        self.pairing_code = None
        if success:
            self.announcer.announce(Prompt.PAIRING_DONE)

    def save_position(self) -> None:
        """Persist the current position, e.g. on shutdown (CLAUDE.md rule 5)."""
        self._save(emit=False)

    # --- state ------------------------------------------------------------------------------

    def status(self) -> Status:
        s = self.session
        if s is None or s.finished:
            state: PlaybackStatus = "stopped"
        else:
            state = "playing" if s.playing else "paused"
        return Status(
            status=state,
            token_id=s.plan.token_id if s and not s.finished else None,
            volume=self._effective(),
        )

    # --- internals --------------------------------------------------------------------------

    def prompt_volume(self) -> int:
        return volume.prompt_volume(
            self.requested_volume, self.config(), self.clock.now(), self.clock.time_trusted()
        )

    def _effective(self) -> int:
        return volume.effective(
            self.requested_volume, self.config(), self.clock.now(), self.clock.time_trusted()
        )

    def _apply_volume(self) -> None:
        eff = self._effective()
        if eff != self._applied_volume:
            self.player.set_volume(eff)
            self._applied_volume = eff

    def _change_volume(self, delta: int) -> None:
        lim = volume.limits(self.config(), self.clock.now(), self.clock.time_trusted())
        # Never build up hidden headroom above the current ceiling.
        self.requested_volume = max(0, min(self.requested_volume + delta, lim.ceiling, 100))
        self._apply_volume()

    def _start(self, plan: Playable) -> None:
        cfg = self.config()
        if volume.limits(cfg, self.clock.now(), self.clock.time_trusted()).locked:
            self.announcer.announce(Prompt.QUIET_TIME)
            return
        if self.session is not None and not self.session.finished:
            self._save(emit=True)
        n = len(plan.items)
        start = ResumePoint(0, 0)
        if plan.resume:
            saved = self.resume_store.get(plan.token_id)
            if saved is not None and 0 <= saved.item_index < n:
                start = saved
        order = list(range(n))
        if plan.shuffle:
            self.rng.shuffle(order)
            order.remove(start.item_index)
            order.insert(0, start.item_index)
        now = self.clock.monotonic()
        s = Session(plan=plan, order=order, play_started=now, last_saved=now)
        self.session = s
        self.requested_volume = cfg.start_volume
        self._apply_volume()
        self.announcer.announce(Prompt.TONE_START)
        self.player.play(s.sources(), order.index(start.item_index), start.position_ms, plan.repeat)
        self.outbox.emit(
            "token_played", TokenPlayedData(token_id=plan.token_id, content_id=plan.content_id)
        )

    def _play_pause(self) -> None:
        if self.pairing_code:
            self._announce_code()
            return
        s = self.session
        if s is not None and not s.finished:
            if s.playing:
                self._pause(s)
                return
            if volume.limits(self.config(), self.clock.now(), self.clock.time_trusted()).locked:
                self.announcer.announce(Prompt.QUIET_TIME)
                return
            self.player.resume()
            s.playing = True
            s.play_started = self.clock.monotonic()
            return
        if self.current_uid is not None:
            self.token_placed(self.current_uid)
            return
        self.announcer.announce(Prompt.TONE_ERROR)  # nothing to play: never silent

    def _pause(self, s: Session) -> None:
        self._save(emit=True)
        self.player.pause()
        s.playing = False

    def _next(self) -> None:
        s = self.session
        if s is None or s.finished:
            self.announcer.announce(Prompt.TONE_ERROR)
            return
        pos = self.player.position()
        current = pos.item_index if pos else 0
        if current + 1 < len(s.order):
            self.player.play(s.sources(), current + 1, 0, s.plan.repeat)
        elif s.plan.repeat == "all":
            self.player.play(s.sources(), 0, 0, s.plan.repeat)
        else:
            self._finish(s)
            return
        s.playing = True
        self._save(emit=False)

    def _finish(self, s: Session) -> None:
        self.player.stop()
        s.playing = False
        s.finished = True
        if s.plan.resume:
            self.resume_store.save(s.plan.token_id, ResumePoint(0, 0))
        self.outbox.emit(
            "resume_position",
            ResumePositionData(token_id=s.plan.token_id, item_index=0, position_ms=0),
        )

    def _save(self, *, emit: bool) -> None:
        s = self.session
        if s is None or s.finished:
            return
        pos = self.player.position()
        if pos is None:
            return
        playlist_index = min(max(pos.item_index, 0), len(s.order) - 1)
        point = ResumePoint(s.order[playlist_index], pos.position_ms)
        s.last_saved = self.clock.monotonic()
        if s.plan.resume:
            self.resume_store.save(s.plan.token_id, point)
        if emit:
            self.outbox.emit(
                "resume_position",
                ResumePositionData(
                    token_id=s.plan.token_id,
                    item_index=point.item_index,
                    position_ms=point.position_ms,
                ),
            )

    def _announce_code(self) -> None:
        if not self.pairing_code:
            return
        self._last_code_announce = self.clock.monotonic()
        self.announcer.announce(
            Prompt.TONE_ATTENTION,
            Prompt.PAIRING_INTRO,
            *(digit_prompt(d) for d in self.pairing_code),
        )
