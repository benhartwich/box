"""Round trips against the JSON examples in docs/SPEC.md (v0.3).

Placeholders like "..." in the spec are replaced with concrete, valid values.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel, TypeAdapter, ValidationError

from myboxi_protocol.auth import DeviceTokenRequest, DeviceTokenResponse
from myboxi_protocol.envelope import RawEnvelope
from myboxi_protocol.errors import ErrorResponse
from myboxi_protocol.events import EventBatchRequest, EventBatchResponse, event_adapter
from myboxi_protocol.messages import CmdAckMessage, CmdMessage, NotifyMessage
from myboxi_protocol.pairing import (
    ClaimRequest,
    ClaimResponse,
    PairingClaimed,
    PairingPending,
    PairingStartRequest,
    PairingStartResponse,
)
from myboxi_protocol.reported import ReportedData, ReportedMessage
from myboxi_protocol.state import DeviceConfig, QuietHours, StateResponse
from myboxi_protocol.updates import UpdateManifest, is_newer

DEV = "0192f3a4-5b6c-7d8e-9f01-23456789abcd"
TENANT = "0192f3a4-0000-7000-8000-000000000001"
TOKEN = "0192f3a4-0000-7000-8000-000000000002"
CONTENT = "0192f3a4-0000-7000-8000-000000000003"
BOOT = "6f1c2a0e-8d7b-4c5a-9e3f-1a2b3c4d5e6f"
ULID = "01J8Z3M5W6XK2C4B7N9P0QRSTV"
SHA = "a" * 64


def roundtrip[M: BaseModel](model: type[M], payload: dict[str, Any]) -> M:
    parsed = model.model_validate_json(json.dumps(payload))
    again = model.model_validate_json(parsed.model_dump_json(exclude_none=True))
    assert again == parsed
    return parsed


def test_envelope_6_0() -> None:
    env = roundtrip(
        RawEnvelope,
        {"v": 1, "id": ULID, "ts": "2026-09-24T18:02:11Z", "type": "x", "data": {}},
    )
    assert env.ts.utcoffset() is not None


def test_notify_6_1() -> None:
    msg = roundtrip(
        NotifyMessage,
        {
            "v": 1,
            "id": ULID,
            "ts": "2026-09-24T18:02:11Z",
            "type": "config_changed",
            "data": {"config_rev": 143, "device_rev": 7},
        },
    )
    assert msg.data.config_rev == 143


def test_cmd_6_2_and_ack_6_3() -> None:
    cmd = roundtrip(
        CmdMessage,
        {
            "v": 1,
            "id": ULID,
            "ts": "2026-09-24T18:02:11Z",
            "type": "cmd",
            "data": {"name": "stop", "args": {}, "expires_at": "2026-09-24T18:03:11Z"},
        },
    )
    assert cmd.data.name == "stop"
    vol = CmdMessage.model_validate(
        {
            "id": ULID,
            "ts": "2026-09-24T18:02:11Z",
            "data": {
                "name": "set_volume",
                "args": {"volume": 30},
                "expires_at": "2026-09-24T18:03:11Z",
            },
        }
    )
    assert vol.data.name == "set_volume"
    with pytest.raises(ValidationError):
        CmdMessage.model_validate(
            {
                "id": ULID,
                "ts": "2026-09-24T18:02:11Z",
                "data": {"name": "set_volume", "args": {}, "expires_at": "2026-09-24T18:03:11Z"},
            }
        )
    roundtrip(
        CmdAckMessage,
        {
            "v": 1,
            "id": ULID,
            "ts": "2026-09-24T18:02:12Z",
            "type": "cmd_ack",
            "data": {"cmd_id": ULID, "result": "ok"},
        },
    )


def test_reported_6_4() -> None:
    msg = roundtrip(
        ReportedMessage,
        {
            "v": 1,
            "id": ULID,
            "ts": "2026-09-24T18:02:11Z",
            "type": "reported",
            "data": {
                "agent_version": "0.3.1",
                "image_version": "2026.09.1",
                "hw_model": "rpi-zero2w",
                "applied_config_rev": 143,
                "applied_device_rev": 7,
                "battery": {"percent": 72, "charging": False},
                "storage": {"free_mb": 9120},
                "wifi_rssi": -61,
                "time_trusted": True,
                "playback": {"status": "playing", "token_id": TOKEN, "volume": 35},
                "soloist": {"installed": True, "build_expires_at": "2026-12-01"},
                "health": [
                    {"check": "nfc", "level": "ok", "code": "ok"},
                    {"check": "audio", "level": "fail", "code": "no_output"},
                ],
                "button_test": {"seen": ["play_pause", "volume_up"]},
                "update": {"state": "waiting", "version": "0.3.0"},
            },
        },
    )
    assert msg.data.soloist is not None
    assert msg.data.soloist.build_expires_at is not None
    assert msg.data.health is not None
    assert [h.code for h in msg.data.health] == ["ok", "no_output"]


def test_reported_without_v06_fields_and_with_unknown_checks() -> None:
    """SPEC v0.6 fields are optional; unknown check names and codes are accepted."""
    base = {
        "agent_version": "0.1.0",
        "hw_model": "rpi4",
        "applied_config_rev": 0,
        "applied_device_rev": 0,
        "storage": {"free_mb": 1},
        "time_trusted": False,
        "playback": {"status": "stopped", "volume": 30},
    }
    assert ReportedData.model_validate(base).health is None
    data = ReportedData.model_validate(
        {**base, "health": [{"check": "battery_gauge", "level": "warn", "code": "future_code"}]}
    )
    assert data.health is not None
    with pytest.raises(ValidationError):
        ReportedData.model_validate(
            {**base, "health": [{"check": "nfc", "level": "ok", "code": "PN532 at /dev/i2c-1"}]}
        )


@pytest.mark.parametrize(
    ("type_", "data"),
    [
        ("token_unknown", {"uid": "04A2B3C4D5E680"}),
        ("token_played", {"token_id": TOKEN, "content_id": CONTENT}),
        ("playback_error", {"token_id": TOKEN, "provider": "spotify", "code": "offline"}),
        ("storage_full", {"needed_mb": 120, "free_mb": 40}),
        ("sync_error", {"stage": "asset_download", "code": "sha_mismatch"}),
        ("resume_position", {"token_id": TOKEN, "item_index": 2, "position_ms": 81234}),
    ],
)
def test_events_6_5(type_: str, data: dict[str, Any]) -> None:
    payload = {
        "v": 1,
        "id": ULID,
        "ts": "2026-09-24T18:02:11Z",
        "type": type_,
        "boot_id": BOOT,
        "mono_ms": 81234,
        "data": data,
    }
    event = event_adapter.validate_json(json.dumps(payload))
    assert event.type == type_
    assert event_adapter.validate_json(event_adapter.dump_json(event)) == event


def test_event_requires_boot_id_and_mono_ms_5_6() -> None:
    with pytest.raises(ValidationError):
        event_adapter.validate_python(
            {
                "id": ULID,
                "ts": "2026-09-24T18:02:11Z",
                "type": "token_unknown",
                "data": {"uid": "04A2B3C4"},
            }
        )


def test_event_rejects_unknown_type() -> None:
    with pytest.raises(ValidationError):
        event_adapter.validate_python(
            {
                "id": ULID,
                "ts": "2026-09-24T18:02:11Z",
                "type": "listening_minutes",
                "boot_id": BOOT,
                "mono_ms": 1,
                "data": {},
            }
        )


def test_event_batch_7_3() -> None:
    batch = roundtrip(
        EventBatchRequest,
        {
            "events": [
                {
                    "v": 1,
                    "id": ULID,
                    "ts": "2026-09-24T18:02:11Z",
                    "type": "token_unknown",
                    "boot_id": BOOT,
                    "mono_ms": 81234,
                    "data": {"uid": "04A2B3C4D5E680"},
                }
            ]
        },
    )
    assert len(batch.events) == 1
    roundtrip(EventBatchResponse, {"results": [{"id": ULID, "status": "accepted"}]})
    with pytest.raises(ValidationError):
        EventBatchRequest.model_validate({"events": []})
    one: dict[str, Any] = {"id": ULID, "ts": "2026-09-24T18:02:11Z", "type": "x", "data": {}}
    too_many = [one] * 101
    with pytest.raises(ValidationError):
        EventBatchRequest.model_validate({"events": too_many})


def test_pairing_7_1() -> None:
    roundtrip(
        PairingStartRequest,
        {"device_id": DEV, "hw_model": "rpi-zero2w", "agent_version": "0.3.1"},
    )
    roundtrip(
        PairingStartResponse,
        {"code": "471193", "expires_in": 600, "poll_token": "p" * 43},
    )
    roundtrip(ClaimRequest, {"code": "471193", "name": "Kinderzimmer"})
    roundtrip(ClaimResponse, {"device_id": DEV, "name": "Kinderzimmer"})
    roundtrip(PairingPending, {"status": "pending", "expires_in": 412})
    claimed = roundtrip(
        PairingClaimed,
        {
            "device_secret": "s" * 43,
            "tenant_id": TENANT,
            "mqtt": {
                "host": "myboxi.example.org",
                "port": 8883,
                "username": DEV,
                "password": "x" * 20,
            },
        },
    )
    assert claimed.mqtt is not None
    without = roundtrip(PairingClaimed, {"device_secret": "s" * 43, "tenant_id": TENANT})
    assert "mqtt" not in without.model_dump(mode="json")


def test_secrets_not_in_repr() -> None:
    claimed = PairingClaimed(device_secret="topsecret-value-123", tenant_id=TENANT)  # pyright: ignore[reportArgumentType]
    assert "topsecret" not in repr(claimed)
    req = DeviceTokenRequest(device_id=DEV, device_secret="topsecret-value-123")  # pyright: ignore[reportArgumentType]
    assert "topsecret" not in repr(req)


def test_token_7_2() -> None:
    roundtrip(DeviceTokenRequest, {"device_id": DEV, "device_secret": "s" * 43})
    resp = roundtrip(
        DeviceTokenResponse, {"access_token": "a.b.c", "token_type": "Bearer", "expires_in": 3600}
    )
    assert resp.token_type == "Bearer"


def test_error_7_4() -> None:
    roundtrip(
        ErrorResponse, {"error": {"code": "pairing_expired", "message": "Pairing code expired"}}
    )


def _state_example() -> dict[str, Any]:
    return {
        "v": 1,
        "full": False,
        "config_rev": 142,
        "device_rev": 7,
        "upserts": {
            "token": [{"id": TOKEN, "uid": "04A2B3C4D5E680", "label": "Bibi"}],
            "content": [
                {
                    "id": CONTENT,
                    "kind": "collection",
                    "title": "Bibi Folge 1",
                    "rev": 3,
                    "source": {},
                }
            ],
            "content_item": [
                {
                    "content_id": CONTENT,
                    "position": 0,
                    "asset_sha256": SHA,
                    "bytes": 3702144,
                    "title": "Teil 1",
                    "duration_ms": 612000,
                }
            ],
            "binding": [
                {
                    "token_id": TOKEN,
                    "content_id": CONTENT,
                    "resume": True,
                    "shuffle": False,
                    "repeat": "off",
                }
            ],
        },
        "deletes": {"token": [], "content": [], "binding": []},
        "device_config": {
            "max_volume": 55,
            "start_volume": 35,
            "quiet_hours": None,
            "sleep_timer_min": None,
            "on_token_removed": "pause",
            "locale": "de-AT",
            "timezone": "Europe/Vienna",
            "providers_enabled": ["local", "podcast"],
        },
    }


def test_state_delta_5_4() -> None:
    state = roundtrip(StateResponse, _state_example())
    assert state.upserts.content[0].kind == "collection"


def test_state_full_has_no_deletes_5_4() -> None:
    full: dict[str, Any] = _state_example() | {"full": True}
    with pytest.raises(ValidationError):
        StateResponse.model_validate(full)
    del full["deletes"]
    parsed = roundtrip(StateResponse, full)
    assert parsed.deletes is None
    dumped = parsed.model_dump(mode="json")
    assert "deletes" not in dumped
    assert dumped["device_config"]["quiet_hours"] is None  # other nulls stay


def test_content_source_per_kind_3_6() -> None:
    adapter = TypeAdapter(StateResponse)
    example: dict[str, Any] = _state_example() | {"full": True}
    del example["deletes"]
    example["upserts"]["content"] = [
        {
            "id": CONTENT,
            "kind": "podcast",
            "title": "Pod",
            "rev": 1,
            "source": {
                "feed_url": "https://example.org/feed.xml",
                "keep_latest": 5,
                "order": "newest_first",
            },
        },
        {
            "id": CONTENT,
            "kind": "spotify",
            "title": "Album",
            "rev": 1,
            "source": {"uri": "spotify:album:4aawyAB9vmqN3uQ7FjRGTy"},
        },
        {
            "id": CONTENT,
            "kind": "stream",
            "title": "Radio",
            "rev": 1,
            "source": {"url": "https://example.org/live"},
        },
    ]
    state = adapter.validate_python(example)
    assert [c.kind for c in state.upserts.content] == ["podcast", "spotify", "stream"]
    example["upserts"]["content"] = [
        {
            "id": CONTENT,
            "kind": "spotify",
            "title": "x",
            "rev": 1,
            "source": {"uri": "https://open.spotify.com"},
        }
    ]
    with pytest.raises(ValidationError):
        adapter.validate_python(example)


def test_device_config_defaults_3_4() -> None:
    cfg = DeviceConfig()
    assert (cfg.max_volume, cfg.start_volume, cfg.on_token_removed) == (55, 35, "pause")
    assert cfg.providers_enabled == ["local", "podcast"]
    assert cfg.locale == "de-AT"


@pytest.mark.parametrize(
    ("payload", "ok"),
    [
        ({"start": "19:30", "end": "06:30", "max_volume": 25}, True),
        ({"start": "19:30", "end": "06:30", "lock": True}, True),
        ({"start": "19:30", "end": "06:30"}, False),
        ({"start": "19:30", "end": "06:30", "max_volume": 25, "lock": True}, False),
        ({"start": "24:00", "end": "06:30", "max_volume": 25}, False),
    ],
)
def test_quiet_hours_3_4(payload: dict[str, Any], ok: bool) -> None:
    if ok:
        QuietHours.model_validate(payload)
    else:
        with pytest.raises(ValidationError):
            QuietHours.model_validate(payload)


def test_update_manifest_11_1() -> None:
    manifest = roundtrip(
        UpdateManifest,
        {
            "channel": "stable",
            "version": "0.3.0",
            "released_at": "2026-09-25T12:00:00Z",
            "bundle": {
                "url": "https://example.org/myboxi-agent-0.3.0-arm64.tar.xz",
                "sha256": "a" * 64,
                "size": 41234567,
            },
        },
    )
    assert manifest.bundle.size == 41234567


@pytest.mark.parametrize(
    ("candidate", "current", "newer"),
    [
        ("0.3.0", "0.2.0", True),
        ("0.10.0", "0.9.9", True),
        ("0.2.0", "0.2.0", False),
        ("0.1.9", "0.2.0", False),
        ("1.0.0", "garbage", True),
        ("garbage", "0.0.1", False),
    ],
)
def test_update_versions(candidate: str, current: str, newer: bool) -> None:
    assert is_newer(candidate, current) is newer


def test_auto_update_defaults_on() -> None:
    assert DeviceConfig().auto_update is True
    assert DeviceConfig.model_validate({"auto_update": False}).auto_update is False
