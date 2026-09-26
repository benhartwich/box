"""``myboxi-server mqtt``: the one process that talks to the broker (SPEC §6, M2).

- Accounts: boxes paired with a pending MQTT password get their broker account; unpaired
  ones lose it (the web and device APIs only write these wishes into the database).
- ``notify`` (§6.1) when a box's ``config_rev`` or ``device_rev`` changed.
- ``cmd`` (§6.2) from ``device_command``; ``cmd/ack`` (§6.3) updates it.
- ``reported`` (§6.4), ``events`` (§6.5) and ``online`` (§6.6) from the boxes.

Everything is polled from the database every second: simple, restart-safe, and a missed
``notify`` only delays a sync until the box's own poll (§5.2).
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import json
import logging
import ssl
import uuid
from collections.abc import AsyncGenerator, Callable
from contextlib import asynccontextmanager
from typing import Any

import aiomqtt
from pydantic import ValidationError
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from myboxi_protocol.envelope import RawEnvelope
from myboxi_protocol.messages import CmdAckMessage, CmdMessage, NotifyMessage
from myboxi_protocol.reported import ReportedMessage
from myboxi_protocol.topics import OFFLINE, ONLINE, parse, topic
from myboxi_server.auth.secretbox import unseal
from myboxi_server.domain import devices, events
from myboxi_server.ids import ulid
from myboxi_server.models import Device, DeviceCommand, Tenant
from myboxi_server.mqtt import dynsec
from myboxi_server.settings import Settings

log = logging.getLogger(__name__)

PURPOSE = "mqtt-password"
TICK_S = 1.0
RECONNECT_S = 5.0
DYNSEC_TIMEOUT_S = 10.0
SUBSCRIPTIONS = ("reported", "events", "cmd/ack", "online")


def tls_context(settings: Settings) -> ssl.SSLContext | None:
    if not settings.mqtt_tls:
        return None
    return ssl.create_default_context(cafile=settings.mqtt_ca_file)


@asynccontextmanager
async def connect(settings: Settings, identifier: str) -> AsyncGenerator[aiomqtt.Client]:
    if settings.mqtt_host is None or settings.mqtt_password is None:
        raise aiomqtt.MqttError("MQTT is not configured")
    async with aiomqtt.Client(
        hostname=settings.mqtt_host,
        port=settings.mqtt_port,
        username=settings.mqtt_username,
        password=settings.mqtt_password.get_secret_value(),
        identifier=identifier,
        tls_context=tls_context(settings),
        timeout=DYNSEC_TIMEOUT_S,
    ) as client:
        yield client


class DynsecError(Exception):
    pass


def _payload(message: aiomqtt.Message) -> bytes:
    return bytes(message.payload)  # received messages always carry bytes


class Dynsec:
    """Commands to the dynamic security plugin over the service's own connection."""

    def __init__(self, client: aiomqtt.Client) -> None:
        self.client = client
        self._waiting: asyncio.Future[list[dict[str, Any]]] | None = None
        self._lock = asyncio.Lock()

    def response(self, payload: bytes) -> None:
        if self._waiting is not None and not self._waiting.done():
            self._waiting.set_result(dynsec.responses(payload))

    async def run(self, commands: list[dict[str, Any]]) -> None:
        async with self._lock:
            self._waiting = asyncio.get_running_loop().create_future()
            await self.client.publish(dynsec.CONTROL, json.dumps({"commands": commands}), qos=1)
            try:
                responses = await asyncio.wait_for(self._waiting, DYNSEC_TIMEOUT_S)
            except TimeoutError as exc:
                raise DynsecError("no answer from the broker") from exc
            finally:
                self._waiting = None
        if bad := dynsec.failures(commands, responses):
            raise DynsecError("; ".join(bad))


Now = Callable[[], dt.datetime]


class MqttService:
    def __init__(
        self,
        settings: Settings,
        sessionmaker: async_sessionmaker[AsyncSession],
        now: Now | None = None,
    ) -> None:
        self.settings = settings
        self.sessionmaker = sessionmaker
        self.now = now or (lambda: dt.datetime.now(dt.UTC))
        self._revisions: dict[uuid.UUID, tuple[int, int]] = {}

    async def run(self) -> None:
        while True:
            try:
                async with connect(self.settings, "myboxi-server") as client:
                    log.info("mqtt connected")
                    await self.serve(client)
            except aiomqtt.MqttError as exc:
                log.warning("mqtt connection lost", extra={"error": str(exc)})
            await asyncio.sleep(RECONNECT_S)

    async def serve(self, client: aiomqtt.Client) -> None:
        control = Dynsec(client)
        await client.subscribe(dynsec.RESPONSE, qos=1)
        for leaf in SUBSCRIPTIONS:
            await client.subscribe(f"myboxi/v1/+/{leaf}", qos=1)
        try:
            async with asyncio.TaskGroup() as tg:
                tg.create_task(self._listen(client, control))
                tg.create_task(self._loop(client, control))
        except* aiomqtt.MqttError as group:
            raise aiomqtt.MqttError(str(group.exceptions[0])) from None

    async def _listen(self, client: aiomqtt.Client, control: Dynsec) -> None:
        async for message in client.messages:
            name = str(message.topic)
            payload = _payload(message)
            if name == dynsec.RESPONSE:
                control.response(payload)
                continue
            try:
                await self.handle(name, payload)
            except Exception:
                log.exception("mqtt message failed", extra={"topic": name})

    async def _loop(self, client: aiomqtt.Client, control: Dynsec) -> None:
        while True:
            await self.accounts(control)
            await self.notify(client)
            await self.commands(client)
            await asyncio.sleep(TICK_S)

    # --- accounts -------------------------------------------------------------------------

    async def accounts(self, control: Dynsec) -> None:
        async with self.sessionmaker() as db:
            pending = list(
                await db.scalars(
                    select(Device).where(
                        (Device.mqtt_password.is_not(None)) | Device.mqtt_revoke.is_(True)
                    )
                )
            )
            for device in pending:
                try:
                    if device.mqtt_password is not None:
                        password = unseal(self.settings, PURPOSE, device.mqtt_password)
                        if password is None:
                            device.mqtt_password = None
                            continue
                        await control.run(dynsec.provision_box(device.id, password))
                        device.mqtt_password, device.mqtt_provisioned = None, True
                        device.mqtt_revoke = False
                        log.info("mqtt account created", extra={"device_id": str(device.id)})
                    else:
                        await control.run(dynsec.remove_box(device.id))
                        device.mqtt_revoke, device.mqtt_provisioned = False, False
                        device.mqtt_online = None
                        log.info("mqtt account removed", extra={"device_id": str(device.id)})
                except DynsecError as exc:
                    log.warning("mqtt account failed", extra={"error": str(exc)})
            await db.commit()

    # --- notify ---------------------------------------------------------------------------

    async def notify(self, client: aiomqtt.Client) -> None:
        async with self.sessionmaker() as db:
            rows = (
                await db.execute(
                    select(Device.id, Tenant.config_rev, Device.device_rev)
                    .join(Tenant, Tenant.id == Device.tenant_id)
                    .where(Device.mqtt_provisioned.is_(True))
                )
            ).all()
        first = not self._revisions
        for device_id, config_rev, device_rev in rows:
            revs = (int(config_rev), int(device_rev))
            if self._revisions.get(device_id) == revs:
                continue
            known = device_id in self._revisions
            self._revisions[device_id] = revs
            if first or not known:
                continue  # the box asks for the state when it connects anyway (SPEC §5.2)
            message = NotifyMessage.model_validate(
                {"id": ulid(), "ts": self.now(),
                 "data": {"config_rev": revs[0], "device_rev": revs[1]}}
            )  # fmt: skip
            await client.publish(topic(device_id, "notify"), message.model_dump_json(), qos=1)

    # --- commands -------------------------------------------------------------------------

    async def commands(self, client: aiomqtt.Client) -> None:
        now = self.now()
        async with self.sessionmaker() as db:
            await db.execute(
                update(DeviceCommand)
                .where(DeviceCommand.sent_at.is_(None), DeviceCommand.result.is_(None),
                       DeviceCommand.expires_at <= now)
                .values(result="expired")
            )  # fmt: skip
            due = list(
                await db.scalars(
                    select(DeviceCommand)
                    .where(DeviceCommand.sent_at.is_(None), DeviceCommand.result.is_(None))
                    .order_by(DeviceCommand.created_at)
                )
            )
            for cmd in due:
                message = CmdMessage.model_validate(
                    {"id": cmd.id, "ts": now,
                     "data": {"name": cmd.name, "args": cmd.args, "expires_at": cmd.expires_at}}
                )  # fmt: skip
                await client.publish(topic(cmd.device_id, "cmd"), message.model_dump_json(), qos=1)
                cmd.sent_at = now
            await db.commit()

    # --- from the boxes -------------------------------------------------------------------

    async def handle(self, name: str, payload: bytes) -> None:
        """One message on ``myboxi/v1/<device>/<leaf>``; the broker's ACL guarantees that
        only that box could publish it (SPEC §6)."""
        parsed = parse(name)
        if parsed is None:
            return
        device_id, leaf = parsed
        async with self.sessionmaker() as db:
            device = await db.get(Device, device_id, with_for_update=True)
            if device is None or device.tenant_id is None:
                return
            now = self.now()
            match leaf:
                case "online":
                    device.mqtt_online = payload == ONLINE if payload in (ONLINE, OFFLINE) else None
                    if device.mqtt_online:
                        device.last_seen_at = now
                case "reported":
                    try:
                        reported = ReportedMessage.model_validate_json(payload)
                    except ValidationError:
                        return
                    devices.store_reported(device, reported.data, now)
                case "events":
                    try:
                        raw = RawEnvelope.model_validate_json(payload)
                    except ValidationError:
                        return
                    await events.ingest(
                        db, tenant_id=device.tenant_id, device_id=device.id, events=[raw]
                    )
                    device.last_seen_at = now
                case "cmd/ack":
                    try:
                        ack = CmdAckMessage.model_validate_json(payload)
                    except ValidationError:
                        return
                    await db.execute(
                        update(DeviceCommand)
                        .where(DeviceCommand.id == ack.data.cmd_id,
                               DeviceCommand.device_id == device.id)
                        .values(result=ack.data.result, message=ack.data.message, acked_at=now)
                    )  # fmt: skip
                case _:
                    return
            await db.commit()


async def _server_roles(settings: Settings) -> set[str]:
    async with connect(settings, "myboxi-server-setup") as client:
        await client.subscribe(dynsec.RESPONSE, qos=1)
        get = [{"command": "getClient", "username": settings.mqtt_username}]
        await client.publish(dynsec.CONTROL, json.dumps({"commands": get}), qos=1)
        async with asyncio.timeout(DYNSEC_TIMEOUT_S):
            async for message in client.messages:
                return dynsec.client_roles(_payload(message))
    return set()


async def setup_server_role(settings: Settings) -> None:
    """Once per broker (``myboxi-server mqtt-setup``): the server's account may send and
    receive under ``myboxi/v1/#``. Nothing happens if it already can. Mosquitto disconnects a
    client whose roles change, so the answer may not arrive; a second connection checks."""
    if dynsec.SERVER_ROLE in await _server_roles(settings):
        return
    commands = dynsec.server_role(settings.mqtt_username)
    with contextlib.suppress(aiomqtt.MqttError):
        async with connect(settings, "myboxi-server-setup") as client:
            await client.subscribe(dynsec.RESPONSE, qos=1)
            await client.publish(dynsec.CONTROL, json.dumps({"commands": commands}), qos=1)
            with contextlib.suppress(TimeoutError):
                async with asyncio.timeout(3):
                    async for _ in client.messages:
                        break
    if dynsec.SERVER_ROLE not in await _server_roles(settings):
        raise DynsecError("the server role is missing")
