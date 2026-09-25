"""Structured logging to stdout (journald) with a secret filter.

Code must never log the device secret or a Soloist key (CLAUDE.md, SPEC §10). The redaction
here is defense in depth for accidental leaks through exception messages or libraries.
"""

from __future__ import annotations

import json
import logging
import re
import sys
from datetime import UTC, datetime
from typing import Any, cast

REDACTED = "[REDACTED]"

# Keys whose values are always secret, in any casing.
_SECRET_KEYS = (
    "device_secret",
    "password",
    "poll_token",
    "access_token",
    "authorization",
    "cookie",
    "set-cookie",
    "secret",
    "token",
    "csrf_token",
    "smtp_password",
    "soloist_key",
    "soloist_api_key",
    "api_key",
)
_KEY_ALT = "|".join(re.escape(k) for k in sorted(_SECRET_KEYS, key=len, reverse=True))
# key=value, key: value, "key": "value", ?key=value&...
_PATTERNS = (
    # HTTP header lines: the whole value up to the end of the line.
    re.compile(r"(\b(?:authorization|cookie|set-cookie)\s*:\s*)(?!\[REDACTED\])[^\r\n]+", re.I),
    re.compile(rf'("(?:{_KEY_ALT})"\s*:\s*)"[^"]*"', re.IGNORECASE),
    re.compile(rf"('(?:{_KEY_ALT})'\s*:\s*)'[^']*'", re.IGNORECASE),
    re.compile(rf"(\b(?:{_KEY_ALT})\s*[=:]\s*)(?!\[REDACTED\])[^\s&,;'\"}}]+", re.IGNORECASE),
    re.compile(r"(\bBearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE),
    # Soloist's command line (SPEC v0.9 §10): --api-key KEY, -k KEY
    re.compile(r"((?:--api-key|(?<!\w)-k)[=\s]+)(?!\[REDACTED\])\S+"),
    re.compile(r"(\$argon2id?\$)[^\s'\"]+"),
)

_STD_ATTRS = frozenset(
    logging.LogRecord("", 0, "", 0, "", (), None).__dict__.keys()
    | {"message", "asctime", "color_message"}
)


def redact(text: str) -> str:
    for pattern in _PATTERNS:
        text = pattern.sub(lambda m: m.group(1) + REDACTED, text)
    return text


def redact_value(key: str, value: Any) -> Any:
    if key.lower() in _SECRET_KEYS:
        return REDACTED
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        mapping = cast(dict[Any, Any], value)
        return {str(k): redact_value(str(k), v) for k, v in mapping.items()}
    if isinstance(value, list | tuple):
        items = cast(list[Any] | tuple[Any, ...], value)
        return [redact_value("", v) for v in items]
    return value


def _extras(record: logging.LogRecord) -> dict[str, Any]:
    return {
        k: redact_value(k, v)
        for k, v in record.__dict__.items()
        if k not in _STD_ATTRS and not k.startswith("_")
    }


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "ts": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": redact(record.getMessage()),
        }
        payload.update(_extras(record))
        if record.exc_info:
            payload["exc"] = redact(self.formatException(record.exc_info))
        return json.dumps(payload, default=str, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        extras = _extras(record)
        line = f"{record.levelname:<7} {record.name}: {redact(record.getMessage())}"
        if extras:
            line += " " + " ".join(f"{k}={v}" for k, v in extras.items())
        if record.exc_info:
            line += "\n" + redact(self.formatException(record.exc_info))
        return line


def configure_logging(level: str = "INFO", fmt: str = "json") -> None:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter() if fmt == "json" else ConsoleFormatter())
    root = logging.getLogger()
    root.handlers[:] = [handler]
    root.setLevel(level.upper())
    # httpx logs full request URLs at INFO, including the pairing poll token (SPEC §7.1).
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
