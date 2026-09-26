"""Sync engine (SPEC §5.2, §5.3, §4.1, §7, §9.5).

Loop: pair if needed (announcing the code), then every 15 minutes or when triggered:
token → state → device_config at once → stage the library → download bound assets
(SHA-256 verified, LRU eviction, ``storage_full``) → activate atomically → events → reported.
Offline never blocks playback (CLAUDE.md rule 1): this loop only waits and retries.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import logging
import math
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Protocol

from myboxi_agent import __version__
from myboxi_agent.adapters.outbox import EventOutbox
from myboxi_agent.core.clock import Clock
from myboxi_agent.core.controller import Controller
from myboxi_agent.core.setup_phase import SetupPhase
from myboxi_agent.ids import ulid
from myboxi_agent.store.repos import AssetRepo, LibraryRepo, OutboxRepo, StateRepo
from myboxi_agent.sync.client import ApiError, ChecksumMismatch, Unreachable
from myboxi_protocol.errors import ErrorCode
from myboxi_protocol.events import EventBatchResponse, StorageFullData, SyncErrorData
from myboxi_protocol.pairing import (
    PairingClaimed,
    PairingPending,
    PairingStartRequest,
    PairingStartResponse,
)
from myboxi_protocol.reported import ReportedData, ReportedMessage
from myboxi_protocol.state import StateResponse

log = logging.getLogger(__name__)

POLL_S = 3.0  # SPEC §7.1
REPORT_CHECK_S = 30.0  # SPEC §6.4: at most every 30 s
SETUP_REPORT_S = 5.0  # SPEC v0.6 §9.6: in the setup phase at most every 5 s
FAST_POLL_S = 30.0
FAST_POLL_FOR_S = 10 * 60
REPORT_MAX_AGE_S = 600.0  # SPEC §6.4: at least every 10 min
BACKOFF_MAX_S = 300.0
DISK_RESERVE = 200 * 1024 * 1024
MB = 1024 * 1024


class Api(Protocol):
    async def pairing_start(self, body: PairingStartRequest) -> PairingStartResponse: ...
    async def pairing_poll(self, poll_token: str) -> PairingPending | PairingClaimed: ...
    async def token(self, device_id: Any, secret: str) -> str: ...
    def forget_token(self) -> None: ...
    async def state(self, token: str, config_rev: int, device_rev: int) -> StateResponse: ...
    async def events(self, token: str, envelopes: list[dict[str, Any]]) -> EventBatchResponse: ...
    async def reported(self, token: str, message: ReportedMessage) -> None: ...
    async def unpair(self, token: str) -> None: ...
    async def download(self, token: str, sha256: str, dest: Any) -> int: ...
    async def aclose(self) -> None: ...


class MessageBus(Protocol):
    """The MQTT link (SPEC §6); HTTPS stays the fallback (§7.3)."""

    def connected(self) -> bool: ...
    def events_pending(self) -> None: ...
    async def publish_reported(self, message: ReportedMessage) -> bool: ...


class NeedsPairing(Exception):
    """Credentials missing or rejected (e.g. the box was removed in the app)."""


class PairingExpired(Exception):
    pass


@dataclass
class SyncStatus:
    last_sync: dt.datetime | None = None
    last_error: str | None = None


class SyncEngine:
    def __init__(
        self,
        *,
        state: StateRepo,
        library: LibraryRepo,
        assets: AssetRepo,
        outbox_repo: OutboxRepo,
        outbox: EventOutbox,
        controller: Controller,
        clock: Clock,
        api_factory: Callable[[str], Api],
        default_server_url: str | None,
        hw_model: str,
        reported_data: Callable[[], ReportedData],
        disk_free: Callable[[], int],
        interval_s: float,
        setup_phase: SetupPhase | None = None,
        on_synced: Callable[[], None] | None = None,
    ) -> None:
        self.state = state
        self.library = library
        self.assets = assets
        self.outbox_repo = outbox_repo
        self.outbox = outbox
        self.controller = controller
        self.clock = clock
        self.api_factory = api_factory
        self.default_server_url = default_server_url
        self.hw_model = hw_model
        self.reported_data = reported_data
        self.disk_free = disk_free
        self.interval_s = interval_s
        self.setup_phase = setup_phase
        self.on_synced = on_synced  # e.g. new podcasts: read their feeds (SPEC v0.8 §8.2)
        self.on_paired: Callable[[], None] | None = None  # e.g. connect to the broker
        self.mqtt: MessageBus | None = None  # SPEC §6: reported and events over MQTT
        self.status = SyncStatus()
        self._wake = asyncio.Event()
        self._report_wake = asyncio.Event()
        self._repair = False
        self._api: Api | None = None
        self._api_url: str | None = None
        self._last_report: dict[str, Any] | None = None
        self._last_report_at = -math.inf
        self._storage_full_for: int | None = None
        self._fast_until = -math.inf

    # --- control -----------------------------------------------------------------------------

    def trigger(self) -> None:
        """Sync now (after reconnect, pairing, ``sync_now``)."""
        self._wake.set()

    def fast_poll(self) -> None:
        """An unknown or still loading figure was placed: until MQTT (M2) notifies the box,
        ask every 30 s for 10 minutes so a figure adopted in the app works right away."""
        self._fast_until = self.clock.monotonic() + FAST_POLL_FOR_S
        self._wake.set()

    def report_soon(self) -> None:
        """Something the setup wizard waits for changed (e.g. a button test press)."""
        self._report_wake.set()

    def in_setup_phase(self) -> bool:
        return self.setup_phase is not None and self.setup_phase.active()

    def request_repair(self) -> None:
        """SPEC §9.4: unpair and pair again."""
        self._repair = True
        self._wake.set()

    def server_url(self) -> str | None:
        return self.state.get().server_url or self.default_server_url

    # --- loops -------------------------------------------------------------------------------

    async def run(self) -> None:
        async with asyncio.TaskGroup() as tg:
            tg.create_task(self._main_loop())
            tg.create_task(self._report_loop())

    async def _main_loop(self) -> None:
        backoff = 5.0
        while True:
            url = self.server_url()
            if not url:
                await self._wait(3600)
                continue
            api = await self._api_for(url)
            try:
                if self._repair:
                    await self._do_repair(api)
                if self.state.get().tenant_id is None:
                    await self._pair(api)
                await self.sync_once(api)
                backoff = 5.0
                self.status.last_error = None
                fast = self.clock.monotonic() < self._fast_until or self.in_setup_phase()
                await self._wait(FAST_POLL_S if fast else self.interval_s)
            except PairingExpired:
                continue
            except NeedsPairing:
                log.warning("credentials rejected: pairing again")
                self.state.clear_tenant()
                api.forget_token()
            except Unreachable as exc:
                self._failed(f"unreachable: {exc}")
                await self._wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)
            except (ApiError, ChecksumMismatch) as exc:
                self._failed(str(exc))
                await self._wait(backoff)
                backoff = min(backoff * 2, BACKOFF_MAX_S)

    def _failed(self, message: str) -> None:
        self.status.last_error = message
        if self.controller.pairing_code:
            self.controller.pairing_finished(success=False)
        log.warning("sync failed", extra={"error": message})

    async def _wait(self, seconds: float) -> None:
        """Sleep until the timeout or a trigger. A trigger that arrived while a sync was
        still running is kept (cleared only after waking), so it is never lost."""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)
        self._wake.clear()

    async def _api_for(self, url: str) -> Api:
        if self._api is None or self._api_url != url:
            if self._api is not None:
                await self._api.aclose()
            self._api, self._api_url = self.api_factory(url), url
        return self._api

    # --- pairing (SPEC §7.1, §9.5) -----------------------------------------------------------

    async def _pair(self, api: Api) -> None:
        st = self.state.get()
        start = await api.pairing_start(
            PairingStartRequest(
                device_id=st.device_id, hw_model=self.hw_model, agent_version=__version__
            )
        )
        self.controller.pairing_started(start.code)
        deadline = self.clock.monotonic() + start.expires_in
        while self.clock.monotonic() < deadline:
            await asyncio.sleep(POLL_S)
            try:
                result = await api.pairing_poll(start.poll_token)
            except ApiError as exc:
                if exc.code == ErrorCode.RATE_LIMITED:
                    continue
                if exc.code in (
                    ErrorCode.PAIRING_EXPIRED,
                    ErrorCode.PAIRING_CONSUMED,
                    ErrorCode.NOT_FOUND,
                ):
                    break
                raise
            if isinstance(result, PairingClaimed):
                self.state.set_paired(result.tenant_id, result.device_secret)
                self.state.set_mqtt(result.mqtt)  # SPEC §7.1: optional, only with a broker
                api.forget_token()
                if self.setup_phase is not None:
                    self.setup_phase.started()
                self.controller.pairing_finished(success=True)
                if self.on_paired is not None:
                    self.on_paired()
                log.info("paired", extra={"tenant_id": str(result.tenant_id)})
                return
        self.controller.pairing_code = None
        raise PairingExpired

    async def _do_repair(self, api: Api) -> None:
        self._repair = False
        secret = self.state.device_secret()
        if secret is not None:
            with contextlib.suppress(ApiError, Unreachable):
                await api.unpair(await api.token(self.state.get().device_id, secret))
        self.state.clear_tenant()
        api.forget_token()

    # --- sync (SPEC §5.2) ----------------------------------------------------------------------

    async def _token(self, api: Api) -> str:
        secret = self.state.device_secret()
        if secret is None:
            raise NeedsPairing
        try:
            return await api.token(self.state.get().device_id, secret)
        except ApiError as exc:
            if exc.code == ErrorCode.INVALID_CREDENTIALS:
                raise NeedsPairing from exc
            raise

    async def sync_once(self, api: Api) -> None:
        st = self.state.get()
        try:
            snapshot = await api.state(
                await self._token(api), st.applied_config_rev, st.applied_device_rev
            )
        except ApiError as exc:
            if exc.status != 401:
                raise
            snapshot = await api.state(
                await self._token(api), st.applied_config_rev, st.applied_device_rev
            )
        if (
            snapshot.device_rev != st.applied_device_rev
            or snapshot.device_config != self.state.device_config()
        ):
            # Limits apply at once, independent of downloads.
            self.state.apply_device_config(snapshot.device_config, snapshot.device_rev)
        if snapshot.config_rev != st.applied_config_rev or self.library.staged() is not None:
            self.library.stage(snapshot)
            if await self._fetch_assets(api, snapshot):
                self.library.activate(snapshot)
                log.info("library activated", extra={"config_rev": snapshot.config_rev})
        if self.on_synced is not None:
            self.on_synced()
        await self.flush_outbox(api)
        await self.report(api, force=True)
        self.status.last_sync = self.clock.now()

    async def _fetch_assets(self, api: Api, snapshot: StateResponse) -> bool:
        """SPEC §4.1: all assets reachable from a binding, completely, before activation."""
        bound = {b.content_id for b in snapshot.upserts.binding}
        collections = {c.id for c in snapshot.upserts.content if c.kind == "collection"}
        needed: dict[str, int] = {}
        for item in snapshot.upserts.content_item:
            if item.content_id in bound and item.content_id in collections:
                needed[item.asset_sha256] = item.bytes
        missing = {sha: size for sha, size in needed.items() if not self.assets.has(sha)}
        if not missing:
            return True
        need = sum(missing.values())
        free = self.disk_free() - DISK_RESERVE
        if need > free:
            free += self._evict(need - free)
        if need > free:
            if self._storage_full_for != snapshot.config_rev:
                self._storage_full_for = snapshot.config_rev
                self.outbox.emit(
                    "storage_full",
                    StorageFullData(needed_mb=math.ceil(need / MB), free_mb=max(free, 0) // MB),
                )
            return False
        for sha in missing:
            try:
                size = await api.download(await self._token(api), sha, self.assets.path(sha))
            except ChecksumMismatch:
                self.outbox.emit(
                    "sync_error", SyncErrorData(stage="asset_download", code="sha_mismatch")
                )
                raise
            except ApiError as exc:
                self.outbox.emit(
                    "sync_error",
                    SyncErrorData(stage="asset_download", code=str(exc.code or exc.status)),
                )
                raise
            self.assets.register(sha, size)
        return True

    def _evict(self, needed: int) -> int:
        return self.assets.evict(needed)

    # --- events and reported ----------------------------------------------------------------

    async def flush_outbox(self, api: Api) -> None:
        if self.mqtt is not None and self.mqtt.connected():
            self.mqtt.events_pending()  # SPEC §6.5: sent over MQTT, removed after PUBACK
            return
        while pending := self.outbox_repo.pending(100):
            result = await api.events(await self._token(api), pending)
            # SPEC §7.3: every listed id leaves the outbox; ``rejected`` is final.
            self.outbox_repo.remove([r.id for r in result.results])
            if len(pending) < 100:
                break

    async def report(self, api: Api, *, force: bool = False) -> None:
        data = self.reported_data()
        # Free space and signal level change all the time; they go along with other changes.
        comparable = data.model_dump(mode="json", exclude={"storage", "wifi_rssi"})
        now = self.clock.monotonic()
        changed = comparable != self._last_report
        if not force and not changed and now - self._last_report_at < REPORT_MAX_AGE_S:
            return
        message = ReportedMessage.model_validate(
            {"id": ulid(), "ts": self.clock.now(), "data": data}
        )
        if self.mqtt is None or not await self.mqtt.publish_reported(message):
            await api.reported(await self._token(api), message)  # SPEC §7.3 fallback
        self._last_report, self._last_report_at = comparable, now

    async def _report_pause(self) -> None:
        if not self.in_setup_phase():
            await asyncio.sleep(REPORT_CHECK_S)
            return
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._report_wake.wait(), SETUP_REPORT_S)
        self._report_wake.clear()
        gap = SETUP_REPORT_S - (self.clock.monotonic() - self._last_report_at)
        if gap > 0:
            await asyncio.sleep(gap)

    async def _report_loop(self) -> None:
        while True:
            await self._report_pause()
            url = self.server_url()
            if not url or self.state.get().tenant_id is None:
                continue
            api = await self._api_for(url)
            with contextlib.suppress(Unreachable, ApiError, NeedsPairing):
                if self.outbox_repo.count():
                    await self.flush_outbox(api)
                await self.report(api)
