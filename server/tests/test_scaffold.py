from __future__ import annotations

import json
import logging
import uuid

import httpx
import pytest
from pydantic import ValidationError

from myboxi_server.cli import build_parser
from myboxi_server.ids import uuid7
from myboxi_server.logconfig import REDACTED, JsonFormatter, redact
from myboxi_server.settings import Settings


def test_uuid7_version_variant_and_order() -> None:
    ids = [uuid7() for _ in range(50)]
    for u in ids:
        assert u.version == 7
        assert u.variant == uuid.RFC_4122
    # Millisecond timestamp prefix is non-decreasing.
    prefixes = [u.int >> 80 for u in ids]
    assert prefixes == sorted(prefixes)


def test_settings_reject_short_jwt_key() -> None:
    with pytest.raises(ValidationError):
        Settings(database_url="postgresql://x@y/z", device_jwt_key="short")  # pyright: ignore[reportArgumentType]


def test_settings_derive_asyncpg_url() -> None:
    s = Settings(database_url="postgresql://u:p@h:5432/db", device_jwt_key="k" * 32)  # pyright: ignore[reportArgumentType]
    assert s.async_database_url == "postgresql+asyncpg://u:p@h:5432/db"


@pytest.mark.parametrize(
    "raw",
    [
        "GET /api/v1/pairing/poll?poll_token=abc123SECRET&x=1",
        '{"device_id": "d", "device_secret": "abc123SECRET"}',
        "{'password': 'abc123SECRET'}",
        "Authorization: Bearer abc123SECRET.part.sig",
        "hash=$argon2id$v=19$m=65536,t=3,p=4$abc123SECRET",
        # prefixed keys (SPEC v0.12): the broker password in an env dump
        "MYBOXI_SERVER_MQTT_PASSWORD=abc123SECRET",
        '{"mqtt_password": "abc123SECRET"}',
        '{"pairing_key": "abc123SECRET"}',
    ],
)
def test_redact_removes_secrets(raw: str) -> None:
    out = redact(raw)
    assert "abc123SECRET" not in out
    assert REDACTED in out


def test_json_formatter_redacts_message_and_extras() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "secret=%s", ("abc123SECRET",), None)
    record.device_secret = "abc123SECRET"
    record.info = {"password": "abc123SECRET", "ok": 1}
    out = json.loads(JsonFormatter().format(record))
    assert "abc123SECRET" not in json.dumps(out)
    assert out["device_secret"] == REDACTED
    assert out["info"]["ok"] == 1


def test_cli_has_required_commands() -> None:
    parser = build_parser()
    for cmd in ("dev", "serve", "worker", "migrate"):
        assert parser.parse_args([cmd]).command == cmd


async def test_healthz(client: httpx.AsyncClient) -> None:
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}
