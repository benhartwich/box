"""Server configuration from environment variables (prefix ``MYBOXI_SERVER_``).

In production the variables come from ``/etc/myboxi-server/myboxi-server.env`` via systemd's
``EnvironmentFile``; in development from ``.dev/env`` (see ``tools/dev-postgres.sh``).
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="MYBOXI_SERVER_", extra="ignore")

    env: Literal["dev", "prod"] = "prod"
    database_url: str = Field(description="postgresql://user:pass@host:port/db")
    base_url: str = "http://localhost:8000"
    # Links in the setup wizard: box images and the hardware guide.
    image_url: str = "https://github.com/benhartwich/myboxi/releases"
    docs_url: str = "https://github.com/benhartwich/myboxi/blob/main/docs"
    # Newest box software (SPEC v0.7 §11.1), shown on the box pages; empty: not shown.
    update_manifest_url: str | None = (
        "https://github.com/benhartwich/myboxi/releases/download/channel-stable/manifest.json"
    )

    # SPEC §7.2: HS256 key for device JWTs; at least 32 bytes.
    device_jwt_key: SecretStr
    device_jwt_ttl_s: int = 3600

    # MQTT (SPEC §6, optional): the broker the MQTT service connects to; boxes get
    # ``mqtt_public_host`` (default: the same). Off while host or password are unset.
    mqtt_host: str | None = None
    mqtt_public_host: str | None = None
    mqtt_port: int = 8883
    mqtt_username: str = "myboxi-server"
    mqtt_password: SecretStr | None = None
    mqtt_tls: bool = True  # SPEC §6: no plain-text port; off only for tests
    mqtt_ca_file: Path | None = None

    # "Box gestalten": in-process cache for generated previews and print files.
    case_cache_mb: int = Field(default=64, ge=0)
    # Order requests for printed cases go here; empty: the request form is off.
    order_notify_email: str | None = None

    data_dir: Path = Path("/var/lib/myboxi-server")
    # nginx internal location for X-Accel-Redirect (SPEC §3.8); None serves files from the app.
    accel_redirect_prefix: str | None = None
    max_upload_mb: int = 500

    session_cookie_name: str = "myboxi_session"
    session_cookie_secure: bool = True
    # Trust X-Real-IP from the reverse proxy (only when listening on a socket behind nginx).
    trust_proxy_headers: bool = False

    mail_backend: Literal["log", "smtp"] = "log"
    mail_from: str = "Myboxi <myboxi@localhost>"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_user: str | None = None
    smtp_password: SecretStr | None = None
    smtp_starttls: bool = True

    ffmpeg_path: str = "ffmpeg"
    ffprobe_path: str = "ffprobe"
    worker_concurrency: int = 1

    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"

    @field_validator("device_jwt_key")
    @classmethod
    def _key_length(cls, v: SecretStr) -> SecretStr:
        if len(v.get_secret_value().encode()) < 32:
            raise ValueError("device_jwt_key must be at least 32 bytes")
        return v

    @field_validator("database_url")
    @classmethod
    def _plain_postgres_url(cls, v: str) -> str:
        if not v.startswith(("postgresql://", "postgres://")):
            raise ValueError("database_url must start with postgresql://")
        return v

    @model_validator(mode="after")
    def _smtp_complete(self) -> Settings:
        if self.mail_backend == "smtp" and not self.smtp_host:
            raise ValueError("smtp_host is required when mail_backend=smtp")
        return self

    @property
    def async_database_url(self) -> str:
        """URL for SQLAlchemy with asyncpg."""
        return "postgresql+asyncpg://" + self.database_url.split("://", 1)[1]

    @property
    def asset_dir(self) -> Path:
        return self.data_dir / "assets"

    @property
    def mqtt_enabled(self) -> bool:
        return bool(self.mqtt_host and self.mqtt_password)

    @property
    def tmp_dir(self) -> Path:
        """Upload staging; on the same filesystem as ``asset_dir`` for atomic renames."""
        return self.data_dir / "tmp"

    @property
    def is_dev(self) -> bool:
        return self.env == "dev"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()  # pyright: ignore[reportCallIssue]  # values come from the environment
