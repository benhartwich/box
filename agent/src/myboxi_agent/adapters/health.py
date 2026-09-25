"""Self-test of the running adapters (SPEC v0.6 §6.4 ``health``).

Adapters record what they observe; ``reported`` and ``doctor`` read it. Only machine codes,
never free text, so nothing like a Wi-Fi name or a path can leak into a report.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Literal

log = logging.getLogger(__name__)

Level = Literal["ok", "warn", "fail"]


@dataclass(frozen=True)
class Result:
    level: Level
    code: str


class Health:
    def __init__(self) -> None:
        self._results: dict[str, Result] = {}

    def set(self, check: str, level: Level, code: str) -> None:
        result = Result(level, code)
        if self._results.get(check) != result:
            logger = log.info if level == "ok" else log.warning
            logger("self-test changed", extra={"check": check, "level": level, "code": code})
        self._results[check] = result

    def ok(self, check: str) -> None:
        self.set(check, "ok", "ok")

    def get(self, check: str) -> Result | None:
        return self._results.get(check)

    def snapshot(self) -> list[dict[str, str]]:
        return [
            {"check": check, "level": r.level, "code": r.code}
            for check, r in sorted(self._results.items())
        ]
