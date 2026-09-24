from __future__ import annotations

import json
import logging

import pytest

from myboxi_agent.cli import main
from myboxi_agent.config import Settings
from myboxi_agent.logconfig import REDACTED, JsonFormatter, redact


def test_defaults_follow_spec_paths() -> None:
    s = Settings()
    assert str(s.db_path) == "/var/lib/myboxi/myboxi.db"  # SPEC §4
    assert str(s.asset_dir) == "/var/lib/myboxi/assets"
    assert s.default_server_url is None  # set by the image, never by accident in dev
    assert s.pins == {"play_pause": 17, "volume_up": 27, "volume_down": 22, "next": 23}


@pytest.mark.parametrize(
    "raw",
    [
        '{"device_secret": "abc123SECRET"}',
        "GET /api/v1/pairing/poll?poll_token=abc123SECRET",
        "soloist_key=abc123SECRET",
        "Authorization: Bearer abc123SECRET.x.y",
    ],
)
def test_secrets_are_redacted(raw: str) -> None:
    out = redact(raw)
    assert "abc123SECRET" not in out
    assert REDACTED in out


def test_json_formatter_redacts_extras() -> None:
    record = logging.LogRecord("t", logging.INFO, __file__, 1, "x", (), None)
    record.device_secret = "abc123SECRET"
    assert json.loads(JsonFormatter().format(record))["device_secret"] == REDACTED


def test_cli_version(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["version"]) == 0
    assert "myboxi-agent" in capsys.readouterr().out
