"""MQTT (SPEC §6, M2) against a real Mosquitto with dynamic security: accounts per box,
ACLs, notify, commands, reported, events, last will, revocation."""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import aiomqtt
import httpx
import pytest
from fastapi import FastAPI
from pydantic import SecretStr
from sqlalchemy import select, update

from myboxi_protocol.messages import CmdMessage, NotifyMessage
from myboxi_protocol.pairing import PairingClaimed
from myboxi_protocol.topics import topic
from myboxi_server.domain import retention
from myboxi_server.models import Device, DeviceCommand, Event, Tenant
from myboxi_server.models.enums import Role
from myboxi_server.mqtt import dynsec
from myboxi_server.mqtt.service import MqttService, setup_server_role
from myboxi_server.settings import Settings

from . import mosquitto
from .helpers import (
    add_member,
    claim_code,
    login,
    make_tenant,
    seed_library,
    sessionmaker_of,
    start_pairing,
)

pytestmark = pytest.mark.skipif(not mosquitto.available(), reason="mosquitto not installed")
BOOT = str(uuid.uuid4())


@pytest.fixture(scope="module")
def broker(tmp_path_factory: pytest.TempPathFactory) -> Any:
    with mosquitto.broker(tmp_path_factory.mktemp("broker")) as b:
        yield b


@pytest.fixture
async def mqtt_settings(settings: Settings, broker: mosquitto.Broker, app: FastAPI) -> Settings:
    configured = settings.model_copy(
        update={
            "mqtt_host": broker.host, "mqtt_port": broker.port,
            "mqtt_password": SecretStr(mosquitto.ADMIN_PASSWORD), "mqtt_tls": False,
        }
    )  # fmt: skip
    await setup_server_role(configured)
    app.state.settings = configured
    return configured


@pytest.fixture
async def service(app: FastAPI, mqtt_settings: Settings) -> AsyncIterator[MqttService]:
    svc = MqttService(mqtt_settings, sessionmaker_of(app))
    task = asyncio.create_task(svc.run())
    await asyncio.sleep(0.3)
    yield svc
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await task


async def eventually(check: Callable[[], Awaitable[bool]], timeout: float = 8.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not await check():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not reached")
        await asyncio.sleep(0.1)


async def pair(app: FastAPI, client: httpx.AsyncClient, tenant_id: uuid.UUID) -> PairingClaimed:
    started = await start_pairing(client)
    await claim_code(app, tenant_id, started.code)
    r = await client.get("/api/v1/pairing/poll", params={"poll_token": started.poll_token})
    assert r.status_code == 200
    return PairingClaimed.model_validate_json(r.content)


async def provisioned(app: FastAPI, device_id: uuid.UUID) -> bool:
    async with sessionmaker_of(app)() as db:
        device = await db.get(Device, device_id)
        return device is not None and device.mqtt_provisioned and device.mqtt_password is None


def box_client(broker: mosquitto.Broker, claimed: PairingClaimed, **kw: Any) -> aiomqtt.Client:
    assert claimed.mqtt is not None
    return aiomqtt.Client(
        broker.host, broker.port, username=claimed.mqtt.username,
        password=claimed.mqtt.password, identifier=claimed.mqtt.username, **kw,
    )  # fmt: skip


async def next_message(client: aiomqtt.Client, timeout: float = 5.0) -> aiomqtt.Message:
    async with asyncio.timeout(timeout):
        async for message in client.messages:
            return message
    raise AssertionError("no message")


# --- unit ----------------------------------------------------------------------------------


def test_box_acl_only_its_own_topics() -> None:
    device = uuid.uuid4()
    commands = dynsec.provision_box(device, "pw")
    acls = {(c["acltype"], c["topic"]) for c in commands if c["command"] == "addRoleACL"}
    assert acls == {
        ("publishClientSend", topic(device, "reported")),
        ("publishClientSend", topic(device, "events")),
        ("publishClientSend", topic(device, "cmd/ack")),
        ("publishClientSend", topic(device, "online")),
        ("subscribePattern", topic(device, "notify")),
        ("subscribePattern", topic(device, "cmd")),
    }
    assert (
        dynsec.failures(commands[:2], [{"error": "Client not found"}, {"error": "Role not found"}])
        == []
    )
    assert dynsec.failures(commands[2:3], [{"error": "Role already exists"}])


# --- with the broker -----------------------------------------------------------------------


async def test_pairing_hands_out_an_own_account_that_only_this_box_can_use(
    app: FastAPI, client: httpx.AsyncClient, broker: mosquitto.Broker, service: MqttService
) -> None:
    t = await make_tenant(app)
    claimed = await pair(app, client, t.tenant_id)
    assert claimed.mqtt is not None
    assert (claimed.mqtt.host, claimed.mqtt.port) == (broker.host, broker.port)
    device_id = uuid.UUID(claimed.mqtt.username)
    async with sessionmaker_of(app)() as db:
        device = await db.get(Device, device_id)
        assert device is not None
        assert device.mqtt_password is None or claimed.mqtt.password.encode() not in (
            device.mqtt_password
        )  # sealed, never in plain text
    await eventually(lambda: provisioned(app, device_id))
    will = aiomqtt.Will(topic(device_id, "online"), b"0", qos=1, retain=True)
    async with box_client(broker, claimed, will=will) as box:
        await box.publish(topic(device_id, "online"), b"1", qos=1, retain=True)

        async def online() -> bool:
            async with sessionmaker_of(app)() as db:
                device = await db.get(Device, device_id)
                return device is not None and device.mqtt_online is True

        await eventually(online)
    wrong = claimed.model_copy(update={"mqtt": claimed.mqtt.model_copy(update={"password": "x"})})
    with pytest.raises(aiomqtt.MqttError):
        async with box_client(broker, wrong):
            pass


async def test_notify_command_ack_reported_and_events(
    app: FastAPI, client: httpx.AsyncClient, broker: mosquitto.Broker, service: MqttService
) -> None:
    t = await make_tenant(app)
    lib = await seed_library(app, t.tenant_id)
    claimed = await pair(app, client, t.tenant_id)
    device_id = uuid.UUID(claimed.mqtt.username) if claimed.mqtt else uuid.uuid4()
    await eventually(lambda: provisioned(app, device_id))
    async with box_client(broker, claimed) as box:
        await box.subscribe(topic(device_id, "notify"), qos=1)
        await box.subscribe(topic(device_id, "cmd"), qos=1)
        await asyncio.sleep(1.5)  # the service has seen the current revisions
        # SPEC §6.1: a change in the app reaches the box at once
        async with sessionmaker_of(app)() as db:
            await db.execute(
                update(Tenant).where(Tenant.id == t.tenant_id)
                .values(config_rev=Tenant.config_rev + 1)
            )  # fmt: skip
            await db.commit()
        notify = NotifyMessage.model_validate_json(bytes((await next_message(box)).payload))
        async with sessionmaker_of(app)() as db:
            tenant = await db.get(Tenant, t.tenant_id)
            assert tenant is not None
            assert notify.data.config_rev == tenant.config_rev
        # SPEC §6.2/§6.3: a command from the app, acknowledged by the box
        csrf = await login(client, t.owner_email)
        r = await client.post(
            f"/t/{t.tenant_id}/boxes/{device_id}/command",
            data={"csrf_token": csrf, "name": "play_token", "token_id": str(lib.token_id)},
        )
        assert r.status_code == 303
        cmd = CmdMessage.model_validate_json(bytes((await next_message(box)).payload))
        assert cmd.data.name == "play_token"
        assert cmd.data.expires_at > dt.datetime.now(dt.UTC)
        ack = {"v": 1, "id": "01J8Z3M5W6XK2C4B7N9P0QRSTV", "ts": "2026-09-26T10:00:00Z",
               "type": "cmd_ack", "data": {"cmd_id": cmd.id, "result": "ok"}}  # fmt: skip
        await box.publish(topic(device_id, "cmd/ack"), json.dumps(ack), qos=1)

        async def acked() -> bool:
            async with sessionmaker_of(app)() as db:
                row = await db.get(DeviceCommand, cmd.id)
                return row is not None and row.result == "ok"

        await eventually(acked)
        page = await client.get(f"/t/{t.tenant_id}/boxes/{device_id}")
        assert "Figur abspielen:" in page.text
        assert "erledigt" in page.text
        # SPEC §6.4, §6.5 over MQTT
        reported = {
            "v": 1, "id": "01J8Z3M5W6XK2C4B7N9P0QRSTW", "ts": "2026-09-26T10:00:00Z",
            "type": "reported",
            "data": {"agent_version": "0.7.0", "hw_model": "rpi-zero2w", "applied_config_rev": 1,
                     "applied_device_rev": 1, "storage": {"free_mb": 100},
                     "time_trusted": True, "playback": {"status": "stopped", "volume": 30}},
        }  # fmt: skip
        await box.publish(topic(device_id, "reported"), json.dumps(reported), qos=1, retain=True)
        event = {"v": 1, "id": "01J8Z3M5W6XK2C4B7N9P0QRSTX", "ts": "2026-09-26T10:00:00Z",
                 "type": "token_unknown", "boot_id": BOOT, "mono_ms": 5,
                 "data": {"uid": "04A2B3C4D5E680"}}  # fmt: skip
        await box.publish(topic(device_id, "events"), json.dumps(event), qos=1)
        # a forged event for another box never arrives (broker ACL)
        await box.publish(topic(uuid.uuid4(), "events"), json.dumps(event), qos=1)

        async def stored() -> bool:
            async with sessionmaker_of(app)() as db:
                device = await db.get(Device, device_id)
                events = (await db.scalars(select(Event))).all()
                return device is not None and device.agent_version == "0.7.0" and len(events) == 1

        await eventually(stored)
        async with sessionmaker_of(app)() as db:
            (only,) = (await db.scalars(select(Event))).all()
            assert only.device_id == device_id


async def test_unpair_removes_the_broker_account(
    app: FastAPI, client: httpx.AsyncClient, broker: mosquitto.Broker, service: MqttService
) -> None:
    t = await make_tenant(app)
    claimed = await pair(app, client, t.tenant_id)
    assert claimed.mqtt is not None
    device_id = uuid.UUID(claimed.mqtt.username)
    await eventually(lambda: provisioned(app, device_id))
    body = {"device_id": str(device_id), "device_secret": claimed.device_secret}
    token = (await client.post("/api/v1/device/token", json=body)).json()["access_token"]
    r = await client.post("/api/v1/device/unpair", headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 204

    async def revoked() -> bool:
        async with sessionmaker_of(app)() as db:
            device = await db.get(Device, device_id)
            return device is not None and not device.mqtt_provisioned and not device.mqtt_revoke

    await eventually(revoked)
    with pytest.raises(aiomqtt.MqttError):
        async with box_client(broker, claimed):
            pass


async def test_commands_need_an_admin_and_valid_arguments(
    app: FastAPI, client: httpx.AsyncClient, service: MqttService
) -> None:
    t = await make_tenant(app)
    other = await make_tenant(app)
    foreign = await seed_library(app, other.tenant_id)
    claimed = await pair(app, client, t.tenant_id)
    assert claimed.mqtt is not None
    device_id = uuid.UUID(claimed.mqtt.username)
    await eventually(lambda: provisioned(app, device_id))
    url = f"/t/{t.tenant_id}/boxes/{device_id}/command"
    csrf = await login(client, t.owner_email)
    r = await client.post(url, data={"csrf_token": csrf, "name": "format_sd_card"})
    assert r.status_code == 400
    r = await client.post(url, data={"csrf_token": csrf, "name": "play_token",
                                     "token_id": str(foreign.token_id)})  # fmt: skip
    assert r.status_code == 404  # only figures of this household
    await client.post("/logout", data={"csrf_token": csrf})
    email = await add_member(app, t.tenant_id, Role.CONTRIBUTOR)
    csrf = await login(client, email)
    r = await client.post(url, data={"csrf_token": csrf, "name": "stop"})
    assert r.status_code == 403


async def test_unsent_commands_expire_and_old_ones_are_purged(
    app: FastAPI, mqtt_settings: Settings
) -> None:
    t = await make_tenant(app)
    device_id = uuid.uuid4()
    now = dt.datetime.now(dt.UTC)
    async with sessionmaker_of(app)() as db:
        db.add(Device(id=device_id, tenant_id=t.tenant_id, hw_model="x", agent_version="1"))
        await db.flush()
        db.add(DeviceCommand(id="01J8Z3M5W6XK2C4B7N9P0QRSTY", tenant_id=t.tenant_id,
                             device_id=device_id, name="stop", args={},
                             expires_at=now - dt.timedelta(seconds=1)))  # fmt: skip
        await db.commit()

    class NoBroker:
        async def publish(self, *args: object, **kwargs: object) -> None:
            raise AssertionError("an expired command must not be sent")

    await MqttService(mqtt_settings, sessionmaker_of(app)).commands(NoBroker())  # type: ignore[arg-type]
    async with sessionmaker_of(app)() as db:
        row = await db.get(DeviceCommand, "01J8Z3M5W6XK2C4B7N9P0QRSTY")
        assert row is not None
        assert row.result == "expired"
        counts = await retention.purge_expired(db, now + dt.timedelta(days=8))
        assert counts["device_command"] == 1


async def test_without_a_broker_no_credentials(
    app: FastAPI, client: httpx.AsyncClient, settings: Settings
) -> None:
    app.state.settings = settings  # MQTT off (SPEC §6: optional)
    t = await make_tenant(app)
    claimed = await pair(app, client, t.tenant_id)
    assert claimed.mqtt is None
