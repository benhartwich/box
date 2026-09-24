"""Recording fakes for the core ports (tests and experiments; never used at runtime)."""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from pydantic import BaseModel

from myboxi_agent.core.model import (
    PlanItem,
    Playable,
    Resolution,
    ResumePoint,
    Unknown,
)
from myboxi_agent.core.ports import EventType
from myboxi_protocol.state import RepeatMode


@dataclass
class FakePlayer:
    sources: list[str] = field(default_factory=list[str])
    index: int = 0
    position_ms: int = 0
    repeat: RepeatMode = "off"
    state: str = "stopped"
    volume: int | None = None
    calls: list[str] = field(default_factory=list[str])

    def play(
        self, sources: Sequence[str], index: int, position_ms: int, repeat: RepeatMode
    ) -> None:
        self.sources, self.index, self.position_ms, self.repeat = (
            list(sources),
            index,
            position_ms,
            repeat,
        )
        self.state = "playing"
        self.calls.append(f"play:{index}@{position_ms}")

    def pause(self) -> None:
        self.state = "paused"
        self.calls.append("pause")

    def resume(self) -> None:
        self.state = "playing"
        self.calls.append("resume")

    def stop(self) -> None:
        self.state = "stopped"
        self.calls.append("stop")

    def set_volume(self, volume: int) -> None:
        self.volume = volume

    def position(self) -> ResumePoint | None:
        if not self.sources:
            return None
        return ResumePoint(self.index, self.position_ms)


@dataclass
class FakeAnnouncer:
    said: list[tuple[str, ...]] = field(default_factory=list[tuple[str, ...]])

    def announce(self, *prompts: str) -> None:
        self.said.append(tuple(str(p) for p in prompts))

    @property
    def flat(self) -> list[str]:
        return [p for group in self.said for p in group]


@dataclass
class FakeOutbox:
    events: list[tuple[EventType, BaseModel]] = field(
        default_factory=list[tuple[EventType, BaseModel]]
    )

    def emit(self, event_type: EventType, data: BaseModel) -> None:
        self.events.append((event_type, data))

    def of(self, event_type: str) -> list[BaseModel]:
        return [d for t, d in self.events if t == event_type]


@dataclass
class FakeResumeStore:
    points: dict[uuid.UUID, ResumePoint] = field(default_factory=dict[uuid.UUID, ResumePoint])

    def get(self, token_id: uuid.UUID) -> ResumePoint | None:
        return self.points.get(token_id)

    def save(self, token_id: uuid.UUID, point: ResumePoint) -> None:
        self.points[token_id] = point


@dataclass
class FakeLibrary:
    by_uid: dict[str, Resolution] = field(default_factory=dict[str, Resolution])

    def resolve(self, uid: str) -> Resolution:
        return self.by_uid.get(uid, Unknown(uid))


@dataclass
class FakeSystem:
    setup_requests: int = 0
    repair_requests: int = 0

    def request_setup_mode(self) -> None:
        self.setup_requests += 1

    def request_repair(self) -> None:
        self.repair_requests += 1


def playable(
    n: int = 3,
    *,
    resume: bool = True,
    shuffle: bool = False,
    repeat: RepeatMode = "off",
    token_id: uuid.UUID | None = None,
) -> Playable:
    return Playable(
        token_id=token_id or uuid.uuid4(),
        content_id=uuid.uuid4(),
        items=tuple(
            PlanItem(source=f"/a/{i}.opus", title=f"T{i}", duration_ms=60_000) for i in range(n)
        ),
        resume=resume,
        shuffle=shuffle,
        repeat=repeat,
    )
