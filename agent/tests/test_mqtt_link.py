"""MQTT on the box (SPEC §6, M2) against a real Mosquitto: online and last will, notify,
commands with expiry, events removed only after the PUBACK, reported, unpairing."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import random
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import aiomqtt
import pytest

from myboxi_agent.adapters.outbox import EventOutbox
from myboxi_agent.core.clock import FakeClock
from myboxi_agent.core.controller import Controller
from myboxi_agent.core.model import PlanItem, Playable, Prompt
from myboxi_agent.store.db import connect
from myboxi_agent.store.repos import Database, OutboxRepo, StateRepo
from myboxi_agent.sync.mqtt import MqttLink
from myboxi_agent.testing import (
    FakeAnnouncer,
    FakeLibrary,
    FakeOutbox,
    FakePlayer,
    FakeResumeStore,
    FakeSystem,
)
from myboxi_protocol.events import TokenUnknownData
from myboxi_protocol.messages import CmdMessage
from myboxi_protocol.pairing import MqttCredentials
from myboxi_protocol.reported import ReportedMessage
from myboxi_protocol.state import DeviceConfig
from myboxi_protocol.topics import topic

from . import mosquitto

pytestmark = pytest.mark.skipif(not mosquitto.available(), reason="mosquitto not installed")
BOX_PASSWORD = "box-test-password-1234"
NOW = dt.datetime(2026, 9, 26, 10, 0, tzinfo=dt.UTC)


@dataclass
class Rig:
    broker: mosquitto.Broker
    db: Database
    link: MqttLink
    device: str
    clock: FakeClock
    notified: list[bool] = field(default_factory=list[bool])
    commands: list[CmdMessage] = field(default_factory=list[CmdMessage])

    def server(self) -> aiomqtt.Client:
        return aiomqtt.Client(self.broker.host, self.broker.port, username=mosquitto.SERVER,
                              password=mosquitto.SERVER_PASSWORD, identifier="server")  # fmt: skip


@pytest.fixture
async def rig(tmp_path: Path) -> AsyncIterator[Rig]:
    clock = FakeClock(NOW)
    db = Database(connect(tmp_path / "myboxi.db"), tmp_path / "assets", clock)
    state = StateRepo(db)
    device = str(state.get().device_id)
    with mosquitto.broker(tmp_path / "broker", device, BOX_PASSWORD) as broker:
        state.set_mqtt(MqttCredentials(host=broker.host, port=broker.port, username=device,
                                       password=BOX_PASSWORD))  # fmt: skip
        holder: dict[str, Rig] = {}

        async def on_command(cmd: CmdMessage) -> tuple[str, str | None]:
            holder["rig"].commands.append(cmd)
            return ("rejected", "unknown_token") if cmd.data.name == "play_token" else ("ok", None)

        link = MqttLink(state=state, outbox=OutboxRepo(db), clock=clock,
                        on_notify=lambda: holder["rig"].notified.append(True),
                        on_command=on_command, tls=False)  # fmt: skip
        rig = Rig(broker, db, link, device, clock)
        holder["rig"] = rig
        task = asyncio.create_task(link.run())
        yield rig
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


async def until(check: Callable[[], bool | Awaitable[bool]], timeout: float = 6.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = check()
        if (isinstance(result, bool) and result) or (not isinstance(result, bool) and await result):
            return
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.05)


async def next_payload(client: aiomqtt.Client, timeout: float = 5.0) -> tuple[str, bytes]:
    async with asyncio.timeout(timeout):
        async for message in client.messages:
            return str(message.topic), bytes(message.payload)
    raise AssertionError("no message")


def cmd(name: str, *, expires: dt.datetime, args: dict[str, Any] | None = None) -> str:
    return json.dumps({"v": 1, "id": "01J8Z3M5W6XK2C4B7N9P0QRSTV", "ts": NOW.isoformat(),
                       "type": "cmd", "data": {"name": name, "args": args or {},
                                               "expires_at": expires.isoformat()}})  # fmt: skip


async def test_online_notify_and_sync_after_connecting(rig: Rig) -> None:
    async with rig.server() as server:
        await server.subscribe(topic(rig.device, "online"), qos=1)
        name, payload = await next_payload(server)  # retained: the box is online
        assert (name, payload) == (topic(rig.device, "online"), b"1")
        await until(rig.link.connected)
        assert rig.notified  # SPEC §5.2: a sync right after connecting
        before = len(rig.notified)
        notify = {"v": 1, "id": "01J8Z3M5W6XK2C4B7N9P0QRSTW", "ts": NOW.isoformat(),
                  "type": "config_changed", "data": {"config_rev": 3, "device_rev": 1}}  # fmt: skip
        await server.publish(topic(rig.device, "notify"), json.dumps(notify), qos=1)
        await until(lambda: len(rig.notified) > before)


async def test_commands_are_acknowledged_and_expired_ones_not_run(rig: Rig) -> None:
    await until(rig.link.connected)
    async with rig.server() as server:
        await server.subscribe(topic(rig.device, "cmd/ack"), qos=1)
        fresh = cmd("stop", expires=NOW + dt.timedelta(seconds=60))
        await server.publish(topic(rig.device, "cmd"), fresh, qos=1)
        _, payload = await next_payload(server)
        assert json.loads(payload)["data"] == {"cmd_id": "01J8Z3M5W6XK2C4B7N9P0QRSTV",
                                               "result": "ok", "message": None}  # fmt: skip
        stale = cmd("stop", expires=NOW - dt.timedelta(seconds=1))
        await server.publish(topic(rig.device, "cmd"), stale, qos=1)
        _, payload = await next_payload(server)
        assert json.loads(payload)["data"]["result"] == "expired"
        assert [c.data.name for c in rig.commands] == ["stop"]  # the expired one never ran
        unknown = cmd("format_sd_card", expires=NOW + dt.timedelta(seconds=60))
        await server.publish(topic(rig.device, "cmd"), unknown, qos=1)
        _, payload = await next_payload(server)
        assert json.loads(payload)["data"]["result"] == "rejected"
        assert len(rig.commands) == 1  # never reached the handler


async def test_without_trusted_time_commands_run(rig: Rig) -> None:
    """The box cannot judge the expiry; the clean session keeps old commands away."""
    rig.clock.trusted = False
    await until(rig.link.connected)
    async with rig.server() as server:
        await server.subscribe(topic(rig.device, "cmd/ack"), qos=1)
        old = cmd("identify", expires=NOW - dt.timedelta(days=1))
        await server.publish(topic(rig.device, "cmd"), old, qos=1)
        _, payload = await next_payload(server)
        assert json.loads(payload)["data"]["result"] == "ok"


async def test_events_leave_the_outbox_after_the_puback(rig: Rig) -> None:
    outbox_repo = OutboxRepo(rig.db)
    outbox = EventOutbox(outbox_repo, rig.clock, uuid.uuid4())
    outbox.on_emit = rig.link.events_pending
    await until(rig.link.connected)
    async with rig.server() as server:
        await server.subscribe(topic(rig.device, "events"), qos=1)
        outbox.emit("token_unknown", TokenUnknownData(uid="04A2B3C4D5E680"))
        name, payload = await next_payload(server)
        assert name == topic(rig.device, "events")
        assert json.loads(payload)["data"] == {"uid": "04A2B3C4D5E680"}
        await until(lambda: outbox_repo.count() == 0)


async def test_reported_is_retained(rig: Rig) -> None:
    await until(rig.link.connected)
    message = ReportedMessage.model_validate(
        {"id": "01J8Z3M5W6XK2C4B7N9P0QRSTX", "ts": NOW,
         "data": {"agent_version": "0.7.0", "hw_model": "rpi-zero2w", "applied_config_rev": 1,
                  "applied_device_rev": 1, "storage": {"free_mb": 1}, "time_trusted": True,
                  "playback": {"status": "stopped", "volume": 30}}}
    )  # fmt: skip
    assert await rig.link.publish_reported(message)
    async with rig.server() as server:
        await server.subscribe(topic(rig.device, "reported"), qos=1)
        _, payload = await next_payload(server)
        assert json.loads(payload)["data"]["agent_version"] == "0.7.0"


async def test_the_box_cannot_publish_as_another_box(rig: Rig) -> None:
    """The broker ACL, from the box's side: its credentials only cover its own topics."""
    other = str(uuid.uuid4())
    async with rig.server() as server:
        await server.subscribe(f"myboxi/v1/{other}/events", qos=1)
        forger_client = aiomqtt.Client(
            rig.broker.host, rig.broker.port, username=rig.device, password=BOX_PASSWORD,
            identifier="forger",
        )  # fmt: skip
        async with forger_client as forger:
            await forger.publish(f"myboxi/v1/{other}/events", b"{}", qos=1)
        with pytest.raises(TimeoutError):
            await next_payload(server, timeout=1.0)


async def test_unpairing_disconnects(rig: Rig) -> None:
    await until(rig.link.connected)
    StateRepo(rig.db).clear_tenant()
    rig.link.trigger()
    await until(lambda: not rig.link.connected(), timeout=40)


# --- commands in the controller ----------------------------------------------------------


def test_remote_stop_and_play_follow_the_rules() -> None:
    """SPEC §6.2: the same limits as buttons and figures (volume policy, quiet hours)."""
    plan = Playable(uuid.uuid4(), uuid.uuid4(), (PlanItem("/a/0.opus", "A", 0),), True, False,
                    "off")  # fmt: skip
    cfg: dict[str, Any] = {"max_volume": 50}
    player, announcer = FakePlayer(), FakeAnnouncer()
    ctl = Controller(
        clock=FakeClock(dt.datetime(2026, 9, 24, 10, 0, tzinfo=dt.UTC)), player=player,
        announcer=announcer, outbox=FakeOutbox(), resume_store=FakeResumeStore(),
        library=FakeLibrary({"04A2B3C4D5E680": plan}), system=FakeSystem(),
        config=lambda: DeviceConfig.model_validate(cfg), rng=random.Random(1),
    )  # fmt: skip
    assert ctl.play_remote("04A2B3C4D5E680") is None
    assert player.calls == ["play:0@0"]
    ctl.token_removed()  # a figure taken off elsewhere does not pause the remote one
    assert player.state == "playing"
    ctl.external_volume(95)
    assert player.volume == 50
    ctl.stop_remote()
    assert player.calls[-1] == "pause"
    assert ctl.play_remote("04FFFFFFFF") == "unknown_token"
    cfg["quiet_hours"] = {"start": "11:00", "end": "13:00", "lock": True}
    assert ctl.play_remote("04A2B3C4D5E680") == "quiet_hours"
    assert Prompt.QUIET_TIME in announcer.flat
