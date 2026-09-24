"""Agent configuration from environment variables (prefix ``MYBOXI_AGENT_``).

On the box they come from ``/etc/myboxi-agent/myboxi-agent.env`` (SPEC §4); the server URL
chosen in setup mode is stored in the database and takes precedence over the default here.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ButtonName = Literal["play_pause", "volume_up", "volume_down", "next"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MYBOXI_AGENT_", extra="ignore")

    data_dir: Path = Path("/var/lib/myboxi")
    default_server_url: str | None = "https://app.myboxi.eu"
    sim: bool = False

    # Hardware (docs/hardware.md): BCM pin numbers, buttons wired to GND.
    pin_play_pause: int = 17
    pin_volume_up: int = 27
    pin_volume_down: int = 22
    pin_next: int = 23
    pn532_i2c_address: int = 0x24
    reader_poll_s: float = 0.2

    mpv_path: str = "mpv"
    audio_output: str = "pipewire"
    prompts_dir: Path = Path("/opt/myboxi-agent/prompts")

    sync_interval_s: int = Field(default=15 * 60, ge=60)
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    @property
    def db_path(self) -> Path:
        return self.data_dir / "myboxi.db"

    @property
    def asset_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def custom_prompts_dir(self) -> Path:
        """Own recordings override the generated prompts (SPEC §4)."""
        return self.data_dir / "prompts"

    @property
    def control_socket(self) -> Path:
        return self.data_dir / "control.sock"

    @property
    def pins(self) -> dict[ButtonName, int]:
        return {
            "play_pause": self.pin_play_pause,
            "volume_up": self.pin_volume_up,
            "volume_down": self.pin_volume_down,
            "next": self.pin_next,
        }


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
