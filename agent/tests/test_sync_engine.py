"""Sync engine against a scripted fake API (SPEC §5.2, §5.3, §4.1, §6.4, §7, §9.5)."""

from __future__ import annotations

import asyncio
import hashlib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest

from myboxi_agent.adapters.bundle import sim_adapters
from myboxi_agent.adapters.sim import SimAnnouncer
from myboxi_agent.app import App
from myboxi_agent.config import Settings
from myboxi_agent.core.model import Playable, Prompt
from myboxi_agent.sync import engine as engine_module
from myboxi_agent.sync.client import ApiError, ChecksumMismatch
from myboxi_protocol.errors import ErrorCode
from myboxi_protocol.events import EventBatchResponse, EventResult
from myboxi_protocol.pairing import (
    PairingClaimed,
    PairingPending,
    PairingStartRequest,
    PairingStartResponse,
)
from myboxi_protocol.reported import ReportedMessage
from myboxi_protocol.state import StateResponse

UID = "04A2B3C4D5E680"
PAYLOAD = b"opus data" * 100
SHA = hashlib.sha256(PAYLOAD).hexdigest()
TENANT = uuid.uuid4()


def snapshot(config_rev: int = 3, max_volume: int = 55, sha: str = SHA) -> StateResponse:
    token, content = uuid.uuid4(), uuid.uuid4()
    return StateResponse.model_validate(
        {
            "full": True, "config_rev": config_rev, "device_rev": 2,
            "upserts": {
                "token": [{"id": str(token), "uid": UID, "label": "Bibi"}],
                "content": [
                    {"id": str(content), "kind": "collection", "title": "F", "rev": 1, "source": {}}
                ],
                "content_item": [{
                    "content_id": str(content), "position": 0, "asset_sha256": sha,
                    "bytes": len(PAYLOAD), "title": "T", "duration_ms": 1000,
                }],
                "binding": [{"token_id": str(token), "content_id": str(content)}],
            },
            "device_config": {"max_volume": max_volume},
        }
    )  # fmt: skip


@dataclass
class FakeApi:
    polls: list[PairingPending | PairingClaimed | ApiError] = field(
        default_factory=list[PairingPending | PairingClaimed | ApiError]
    )
    token_error: ApiError | None = None
    snapshot: StateResponse = field(default_factory=snapshot)
    payload: bytes = PAYLOAD
    starts: int = 0
    events_received: list[dict[str, Any]] = field(default_factory=list[dict[str, Any]])
    reported_sent: list[ReportedMessage] = field(default_factory=list[ReportedMessage])
    unpaired: int = 0
    downloads: int = 0
    reject_ids: set[str] = field(default_factory=set[str])

    async def pairing_start(self, body: PairingStartRequest) -> PairingStartResponse:
        del body
        self.starts += 1
        return PairingStartResponse(code=f"{self.starts:06d}", expires_in=600, poll_token="p" * 40)

    async def pairing_poll(self, poll_token: str) -> PairingPending | PairingClaimed:
        del poll_token
        item = self.polls.pop(0) if self.polls else PairingPending(expires_in=100)
        if isinstance(item, ApiError):
            raise item
        return item

    async def token(self, device_id: Any, secret: str) -> str:
        del device_id, secret
        if self.token_error is not None:
            raise self.token_error
        return "jwt"

    def forget_token(self) -> None:
        pass

    async def state(self, token: str, config_rev: int, device_rev: int) -> StateResponse:
        del token, config_rev, device_rev
        return self.snapshot

    async def events(self, token: str, envelopes: list[dict[str, Any]]) -> EventBatchResponse:
        del token
        self.events_received.extend(envelopes)
        return EventBatchResponse(
            results=[
                EventResult(
                    id=e["id"], status="rejected" if e["id"] in self.reject_ids else "accepted"
                )
                for e in envelopes
            ]
        )

    async def reported(self, token: str, message: ReportedMessage) -> None:
        del token
        self.reported_sent.append(message)

    async def unpair(self, token: str) -> None:
        del token
        self.unpaired += 1

    async def download(self, token: str, sha256: str, dest: Path) -> int:
        del token
        self.downloads += 1
        if hashlib.sha256(self.payload).hexdigest() != sha256:
            raise ChecksumMismatch(sha256)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(self.payload)
        return len(self.payload)

    async def aclose(self) -> None:
        pass


@pytest.fixture(autouse=True)
def fast_polls(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "POLL_S", 0.01)


@pytest.fixture
def api() -> FakeApi:
    return FakeApi()


@pytest.fixture
def app(tmp_path: Path, api: FakeApi) -> App:
    settings = Settings(data_dir=tmp_path, sim=True, default_server_url="https://box.test")
    return App(settings, sim_adapters(), api_factory=lambda _url: api)  # pyright: ignore[reportArgumentType]


def claimed() -> PairingClaimed:
    return PairingClaimed(device_secret="s" * 43, tenant_id=TENANT)


async def run_until(app: App, condition: Any, timeout: float = 5) -> None:
    task = asyncio.create_task(app.sync.run())
    try:
        async with asyncio.timeout(timeout):
            while not condition():
                await asyncio.sleep(0.01)
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_pairing_announces_code_and_stores_credentials(app: App, api: FakeApi) -> None:
    api.polls = [PairingPending(expires_in=100), claimed()]
    await run_until(app, lambda: app.state.get().applied_config_rev == 3)
    assert isinstance(app.announcer, SimAnnouncer)
    said = app.announcer.history
    assert said[0][:2] == (Prompt.TONE_ATTENTION, Prompt.PAIRING_INTRO)
    assert said[0][2:] == tuple(f"digit_{d}" for d in "000001")
    assert (Prompt.PAIRING_DONE,) in said
    assert app.state.get().tenant_id == TENANT
    assert app.state.device_secret() == "s" * 43
    assert app.controller.pairing_code is None


async def test_expired_code_starts_a_new_pairing(app: App, api: FakeApi) -> None:
    api.polls = [ApiError(410, ErrorCode.PAIRING_EXPIRED), claimed()]
    await run_until(app, lambda: app.state.get().tenant_id is not None)
    assert api.starts == 2


async def test_sync_downloads_verifies_and_activates(app: App, api: FakeApi) -> None:
    app.state.set_paired(TENANT, "s" * 43)
    await app.sync.sync_once(api)  # pyright: ignore[reportArgumentType]
    assert app.state.device_config().max_volume == 55
    assert isinstance(app.library.resolve(UID), Playable)
    assert app.assets.path(SHA).read_bytes() == PAYLOAD
    assert app.state.get().applied_config_rev == 3
    await app.sync.sync_once(api)  # pyright: ignore[reportArgumentType]
    assert api.downloads == 1  # nothing new to fetch
    assert api.reported_sent[-1].data.applied_config_rev == 3


async def test_config_applies_before_downloads_complete(app: App, api: FakeApi) -> None:
    """Limits never wait for assets; the library waits (SPEC §5.3)."""
    app.state.set_paired(TENANT, "s" * 43)
    api.snapshot = snapshot(max_volume=30)
    api.payload = b"corrupted"
    with pytest.raises(ChecksumMismatch):
        await app.sync.sync_once(api)  # pyright: ignore[reportArgumentType]
    assert app.state.device_config().max_volume == 30
    assert app.library.staged() is not None
    assert app.state.get().applied_config_rev == 0
    kinds = [e["type"] for e in app.outbox_repo.pending()]
    assert kinds == ["sync_error"]


async def test_storage_full_is_reported_once(app: App, api: FakeApi) -> None:
    app.state.set_paired(TENANT, "s" * 43)
    app.sync.disk_free = lambda: 10  # far below the reserve
    await app.sync.sync_once(api)  # pyright: ignore[reportArgumentType]
    await app.sync.sync_once(api)  # pyright: ignore[reportArgumentType]
    full = [e for e in api.events_received if e["type"] == "storage_full"]
    assert len(full) == 1
    assert full[0]["data"]["needed_mb"] >= 1
    assert api.downloads == 0


async def test_rejected_credentials_trigger_pairing(app: App, api: FakeApi) -> None:
    app.state.set_paired(TENANT, "s" * 43)
    api.token_error = ApiError(401, ErrorCode.INVALID_CREDENTIALS)
    api.polls = [claimed()]

    def repaired() -> bool:
        if api.starts:
            api.token_error = None
        return api.starts >= 1 and app.state.get().applied_config_rev == 3

    await run_until(app, repaired)
    assert app.state.get().tenant_id == TENANT


async def test_outbox_is_emptied_including_rejected(app: App, api: FakeApi) -> None:
    app.state.set_paired(TENANT, "s" * 43)
    app.controller.token_placed("04FFFFFFFF")  # token_unknown
    app.controller.token_placed("04EEEEEEEE")
    rejected = app.outbox_repo.pending()[1]["id"]
    api.reject_ids = {rejected}
    await app.sync.flush_outbox(api)  # pyright: ignore[reportArgumentType]
    assert app.outbox_repo.count() == 0
    assert len(api.events_received) == 2


async def test_reported_only_on_change_or_after_ten_minutes(app: App, api: FakeApi) -> None:
    app.state.set_paired(TENANT, "s" * 43)
    await app.sync.report(api)  # pyright: ignore[reportArgumentType]
    await app.sync.report(api)  # pyright: ignore[reportArgumentType]
    assert len(api.reported_sent) == 1
    app.controller.requested_volume = 10
    await app.sync.report(api)  # pyright: ignore[reportArgumentType]
    assert len(api.reported_sent) == 2


async def test_repair_unpairs_and_pairs_again(app: App, api: FakeApi) -> None:
    app.state.set_paired(TENANT, "s" * 43)
    app.sync.request_repair()
    await run_until(app, lambda: api.starts >= 1)
    assert api.unpaired == 1
    assert app.state.device_secret() is None


def test_changing_the_server_forgets_credentials(app: App) -> None:
    app.state.set_paired(TENANT, "s" * 43)
    app.state.set_server_url("https://other.example")
    assert app.state.device_secret() is None
    assert app.sync.server_url() == "https://other.example"


async def test_offline_does_not_block_playback(tmp_path: Path) -> None:
    """CLAUDE.md rule 1: an unreachable server only delays sync."""
    from myboxi_agent.sync.client import DeviceApi

    settings = Settings(data_dir=tmp_path, sim=True, default_server_url="http://127.0.0.1:9")
    app = App(settings, sim_adapters(), api_factory=DeviceApi)
    token_id = app.library.add_local(UID, "Bibi", "Lokal", [])
    del token_id
    task: asyncio.Task[None] = asyncio.create_task(app.sync.run())
    try:
        await asyncio.sleep(0.3)
        assert app.sync.status.last_error is not None
        assert "unreachable" in app.sync.status.last_error
        app.controller.token_placed(UID)  # still reacts at once
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


async def test_trigger_during_a_running_sync_is_not_lost(app: App, api: FakeApi) -> None:
    """A repair requested while a sync is still running must start right after it."""
    app.state.set_paired(TENANT, "s" * 43)
    app.sync.interval_s = 3600
    original = app.sync.sync_once
    calls = 0

    async def slow_sync(api_: Any) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            app.sync.request_repair()  # arrives while this sync is still in progress
        await original(api_)

    app.sync.sync_once = slow_sync  # type: ignore[method-assign]
    await run_until(app, lambda: api.unpaired == 1 and api.starts >= 1, timeout=3)


async def test_unknown_figure_speeds_up_polling(
    app: App, api: FakeApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Until MQTT (M2): after an unknown figure the box asks every 30 s for 10 minutes."""
    monkeypatch.setattr(engine_module, "FAST_POLL_S", 0.05)
    app.state.set_paired(TENANT, "s" * 43)
    app.sync.interval_s = 3600
    syncs = 0
    original = app.sync.sync_once

    async def counting(api_: Any) -> None:
        nonlocal syncs
        syncs += 1
        await original(api_)

    app.sync.sync_once = counting  # type: ignore[method-assign]
    app.sync.fast_poll()
    await run_until(app, lambda: syncs >= 3, timeout=3)
