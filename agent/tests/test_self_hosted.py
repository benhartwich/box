"""A self-hosted server with its own CA (SPEC v0.13 §9.3, §10): the box trusts it only for
the server connections, and only when it was given the CA."""

from __future__ import annotations

import asyncio
import json
import ssl
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import cast

import pytest

from myboxi_agent.sync.client import DeviceApi, Unreachable
from myboxi_agent.sync.mqtt import tls_context
from myboxi_agent.sync.tls import MAX_CA_PEM, normalize_ca, server_verify, valid_ca
from myboxi_protocol.pairing import PairingStartRequest

from . import owncerts

pytestmark = pytest.mark.skipif(not owncerts.available(), reason="openssl not installed")


@pytest.fixture
def certs(tmp_path: Path) -> owncerts.OwnCerts:
    return owncerts.make(tmp_path / "certs")


@pytest.fixture
async def https_server(certs: owncerts.OwnCerts) -> AsyncIterator[str]:
    """Answers every request like ``POST /pairing/start`` (SPEC §7.1)."""
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(certs.cert, certs.key)
    body = json.dumps({"code": "471193", "expires_in": 600, "poll_token": "p" * 40}).encode()

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readuntil(b"\r\n\r\n")
            length = next(
                (int(line.split(b":")[1]) for line in head.split(b"\r\n")
                 if line.lower().startswith(b"content-length:")), 0,
            )  # fmt: skip
            await reader.readexactly(length)
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: application/json\r\n"
                + f"content-length: {len(body)}\r\nconnection: close\r\n\r\n".encode()
                + body
            )
            await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError, ssl.SSLError):
            pass
        finally:
            writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0, ssl=context)
    port = server.sockets[0].getsockname()[1]
    yield f"https://localhost:{port}"
    server.close()


def common_names(context: ssl.SSLContext) -> list[str]:
    names: list[str] = []
    for cert in context.get_ca_certs():
        subject = cast(tuple[tuple[tuple[str, str], ...], ...], cert.get("subject", ()))
        names += [value for rdn in subject for key, value in rdn if key == "commonName"]
    return names


def start_request() -> PairingStartRequest:
    return PairingStartRequest(device_id=uuid.uuid4(), hw_model="rpi-zero2w",
                               agent_version="0.7.0", pairing_key="K" * 43)  # fmt: skip


def test_only_pem_certificates_are_accepted(certs: owncerts.OwnCerts) -> None:
    pem = certs.ca.read_text()
    assert valid_ca(pem)
    assert valid_ca(pem.replace("\n", "\r\n"))  # pasted from Windows
    assert normalize_ca(pem.replace("\n", "\r\n") + "\r\n\r\n") == pem
    assert not valid_ca("")
    assert not valid_ca("hello")
    assert not valid_ca(certs.key.read_text())  # never a private key
    assert not valid_ca(pem.replace("MII", "XYZ", 1))
    assert not valid_ca(pem * 4)  # at most three certificates
    assert not valid_ca(pem + "x" * MAX_CA_PEM)
    assert not valid_ca(None)


async def test_the_box_trusts_the_own_ca_only_when_given(
    https_server: str, certs: owncerts.OwnCerts
) -> None:
    without = DeviceApi(https_server)
    with pytest.raises(Unreachable):  # certificate verify failed
        await without.pairing_start(start_request())
    await without.aclose()
    api = DeviceApi(https_server, verify=server_verify(certs.ca.read_text()))
    started = await api.pairing_start(start_request())
    await api.aclose()
    assert started.code == "471193"


def test_the_usual_cas_stay_trusted(certs: owncerts.OwnCerts) -> None:
    """app.myboxi.eu keeps working with an own CA stored; the CA is only added."""
    context = server_verify(certs.ca.read_text())
    assert isinstance(context, ssl.SSLContext)
    subjects = common_names(context)
    assert "Myboxi test CA" in subjects
    assert len(subjects) > 50  # certifi's roots as well
    assert server_verify(None) is True


def test_mqtt_uses_the_own_ca_as_well(certs: owncerts.OwnCerts) -> None:
    context = tls_context(ca_pem=certs.ca.read_text())
    subjects = common_names(context)
    assert "Myboxi test CA" in subjects
    assert context.minimum_version == ssl.TLSVersion.TLSv1_2
    assert context.check_hostname


# --- stored per server, set from the setup portal (SPEC v0.13 §9.3) -------------------------


async def test_the_own_ca_belongs_to_its_server(tmp_path: Path, certs: owncerts.OwnCerts) -> None:
    from myboxi_agent.adapters.bundle import sim_adapters
    from myboxi_agent.app import App
    from myboxi_agent.config import Settings

    pem = certs.ca.read_text()
    app = App(Settings(data_dir=tmp_path / "box", default_server_url="https://app.myboxi.eu"),
              sim_adapters())  # fmt: skip
    home = "https://myboxi.home.example"
    answer = await app.handle_control({"cmd": "set_server_url", "url": home, "ca": "junk"})
    assert answer["ok"] is False
    assert app.state.get().server_url is None  # nothing changed
    answer = await app.handle_control({"cmd": "set_server_url", "url": home, "ca": pem})
    assert answer["server_ca_set"] is True
    await app.handle_control({"cmd": "set_server_url", "url": home})  # e.g. only a new WLAN
    assert app.state.server_ca() == pem
    await app.handle_control({"cmd": "set_server_url", "url": home, "ca_clear": True})
    assert app.state.server_ca() is None
    await app.handle_control({"cmd": "set_server_url", "url": home, "ca": pem})
    await app.handle_control({"cmd": "set_server_url", "url": "https://app.myboxi.eu"})
    assert app.state.server_ca() is None  # never trusted for another server
    app.state.clear_tenant()
    app.state.set_server_ca(pem)
    app.state.clear_tenant()  # unpairing keeps the choice of server
    assert app.state.server_ca() == pem


async def test_a_new_ca_means_a_new_client(tmp_path: Path, certs: owncerts.OwnCerts) -> None:
    from myboxi_agent.adapters.bundle import sim_adapters
    from myboxi_agent.app import App
    from myboxi_agent.config import Settings

    app = App(Settings(data_dir=tmp_path / "box"), sim_adapters())
    first = await app.sync._api_for("https://myboxi.home.example")  # pyright: ignore[reportPrivateUsage]
    same = await app.sync._api_for("https://myboxi.home.example")  # pyright: ignore[reportPrivateUsage]
    assert first is same
    app.state.set_server_ca(certs.ca.read_text())
    other = await app.sync._api_for("https://myboxi.home.example")  # pyright: ignore[reportPrivateUsage]
    assert other is not first
    assert isinstance(other, DeviceApi)
    await other.aclose()
