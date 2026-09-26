"""MQTT on the box (SPEC §6, M2): notify, commands, reported, events, last will.

- TLS only, with the system's CAs and host name check (SPEC §6: no plain-text port).
- The client id is the user name (the broker enforces it), so no one can take over the
  session of another box or the server.
- Clean session: nothing queued while the box was offline is delivered later, so an old
  ``play_token`` can never start music hours afterwards (§6.2); ``notify`` is not needed
  after a reconnect because the box asks for the state then anyway (§5.2).
- ``online`` is ``1`` while connected, the last will sets ``0`` (§6.6).
- Events leave the outbox only after the broker's PUBACK (§6.5); ``reported`` is retained.
- Commands: expired ones are acknowledged with ``expired`` and not executed; without a
  trusted clock the box cannot tell, and relies on the clean session.
Without a broker (``mqtt`` missing from pairing) the box keeps using HTTPS only (§6).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import ssl
from collections.abc import Awaitable, Callable
from pathlib import Path

import aiomqtt
from pydantic import ValidationError

from myboxi_agent.core.clock import Clock
from myboxi_agent.ids import ulid
from myboxi_agent.store.repos import OutboxRepo, StateRepo
from myboxi_protocol.messages import CmdAckMessage, CmdMessage, NotifyMessage
from myboxi_protocol.pairing import MqttCredentials
from myboxi_protocol.reported import ReportedMessage
from myboxi_protocol.topics import OFFLINE, ONLINE, topic

log = logging.getLogger(__name__)

KEEPALIVE_S = 60
RETRY_S = 5.0
RETRY_MAX_S = 120.0
OUTBOX_CHECK_S = 2.0
CommandResult = tuple[str, str | None]  # result (ok, expired, rejected, error), message
CommandHandler = Callable[[CmdMessage], Awaitable[CommandResult]]


def tls_context(ca_file: Path | None = None) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context  # verifies the certificate and the host name


class MqttLink:
    def __init__(
        self,
        *,
        state: StateRepo,
        outbox: OutboxRepo,
        clock: Clock,
        on_notify: Callable[[], None],
        on_command: CommandHandler,
        tls: bool = True,
        ca_file: Path | None = None,
    ) -> None:
        self.state = state
        self.outbox = outbox
        self.clock = clock
        self.on_notify = on_notify
        self.on_command = on_command
        self.tls = tls
        self.ca_file = ca_file
        self._client: aiomqtt.Client | None = None
        self._wake = asyncio.Event()
        self._outbox_wake = asyncio.Event()

    def connected(self) -> bool:
        return self._client is not None

    def trigger(self) -> None:
        """Credentials changed (paired, unpaired): connect or disconnect now."""
        self._wake.set()

    def events_pending(self) -> None:
        self._outbox_wake.set()

    async def run(self) -> None:
        delay = RETRY_S
        while True:
            creds = self.state.mqtt()
            if creds is None:
                await self._sleep(3600)
                continue
            try:
                await self._session(creds)
                delay = RETRY_S
            except aiomqtt.MqttError as exc:
                # e.g. the server creates the account a moment after pairing: try again soon
                log.info("mqtt not connected", extra={"error": str(exc)[:200]})
            finally:
                self._client = None
            await self._sleep(delay)
            delay = min(delay * 2, RETRY_MAX_S)

    async def _sleep(self, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(self._wake.wait(), seconds)
        self._wake.clear()

    async def _session(self, creds: MqttCredentials) -> None:
        device = creds.username
        will = aiomqtt.Will(topic(device, "online"), OFFLINE, qos=1, retain=True)
        async with aiomqtt.Client(
            hostname=creds.host,
            port=creds.port,
            username=creds.username,
            password=creds.password,
            identifier=creds.username,  # the broker allows no other (SPEC v0.12 §6)
            tls_context=tls_context(self.ca_file) if self.tls else None,
            will=will,
            clean_session=True,
            keepalive=KEEPALIVE_S,
        ) as client:
            await client.subscribe(topic(device, "notify"), qos=1)
            await client.subscribe(topic(device, "cmd"), qos=1)
            await client.publish(topic(device, "online"), ONLINE, qos=1, retain=True)
            self._client = client
            log.info("mqtt connected")
            self.on_notify()  # SPEC §5.2: sync after (re)connecting
            try:
                async with asyncio.TaskGroup() as tg:
                    tg.create_task(self._messages(client, device))
                    tg.create_task(self._events(client, device))
                    tg.create_task(self._watch_credentials(creds))
            except* aiomqtt.MqttError as group:
                raise aiomqtt.MqttError(str(group.exceptions[0])) from None

    async def _watch_credentials(self, creds: MqttCredentials) -> None:
        """Leave when the box was unpaired or paired anew (other credentials)."""
        while True:
            await self._sleep(30)
            if self.state.mqtt() != creds:
                raise aiomqtt.MqttError("credentials changed")

    # --- to the box -----------------------------------------------------------------------

    async def _messages(self, client: aiomqtt.Client, device: str) -> None:
        async for message in client.messages:
            payload = bytes(message.payload)
            if message.topic.matches(topic(device, "notify")):
                try:
                    NotifyMessage.model_validate_json(payload)
                except ValidationError:
                    continue
                self.on_notify()
            elif message.topic.matches(topic(device, "cmd")):
                await self._command(client, device, payload)

    async def _command(self, client: aiomqtt.Client, device: str, payload: bytes) -> None:
        try:
            cmd = CmdMessage.model_validate_json(payload)
        except ValidationError:
            try:
                cmd_id = str(json.loads(payload)["id"])
            except (ValueError, KeyError, TypeError):
                return
            await self._ack(client, device, cmd_id, "rejected", "invalid")
            return
        if self.clock.time_trusted() and cmd.data.expires_at <= self.clock.now():
            await self._ack(client, device, cmd.id, "expired", None)
            return
        try:
            result, message = await self.on_command(cmd)
        except Exception:
            log.exception("command failed", extra={"name": cmd.data.name})
            result, message = "error", None
        await self._ack(client, device, cmd.id, result, message)

    async def _ack(
        self, client: aiomqtt.Client, device: str, cmd_id: str, result: str, message: str | None
    ) -> None:
        with contextlib.suppress(ValidationError):
            ack = CmdAckMessage.model_validate(
                {"id": ulid(), "ts": self.clock.now(),
                 "data": {"cmd_id": cmd_id, "result": result, "message": message}}
            )  # fmt: skip
            await client.publish(topic(device, "cmd/ack"), ack.model_dump_json(), qos=1)

    # --- from the box ---------------------------------------------------------------------

    async def publish_reported(self, message: ReportedMessage) -> bool:
        client = self._client
        if client is None:
            return False
        device = str(self.state.get().device_id)
        try:
            await client.publish(topic(device, "reported"), message.model_dump_json(), qos=1,
                                 retain=True)  # fmt: skip
        except (aiomqtt.MqttError, TimeoutError) as exc:
            # the connection just broke: the caller reports over HTTPS instead (§7.3)
            log.info("mqtt reported failed", extra={"error": str(exc)[:200]})
            return False
        return True

    async def _events(self, client: aiomqtt.Client, device: str) -> None:
        """SPEC §6.5: one event per message, removed after the PUBACK."""
        while True:
            for envelope in self.outbox.pending(100):
                await client.publish(topic(device, "events"), json.dumps(envelope), qos=1)
                self.outbox.remove([str(envelope["id"])])
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._outbox_wake.wait(), OUTBOX_CHECK_S)
            self._outbox_wake.clear()
