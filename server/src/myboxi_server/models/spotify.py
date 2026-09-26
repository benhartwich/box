"""The household's own Spotify app for searching in the web UI (SPEC v0.10 §3.6).

Not part of the device protocol: the box only ever gets the Spotify URI and plays it with
Soloist (SPEC §8.1). Spotify allows an app in development mode for 5 users whose owner has
Premium, so every household registers its own app; with PKCE only its client id is needed.
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, LargeBinary, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from myboxi_server.models.base import Base, Timestamps


class SpotifyAccount(Timestamps, Base):
    __tablename__ = "spotify_account"

    tenant_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("tenant.id", ondelete="CASCADE"), primary_key=True
    )
    client_id: Mapped[str] = mapped_column(Text)
    # AES-GCM sealed (auth/secretbox.py); None until connected or after Spotify revoked it.
    refresh_token: Mapped[bytes | None] = mapped_column(LargeBinary)
    display_name: Mapped[str | None] = mapped_column(Text)
    connected_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True))
    connected_by: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("app_user.id", ondelete="SET NULL")
    )


class SpotifyLogin(Base):
    """An authorization in progress: the state (hashed), PKCE verifier, 10 minutes."""

    __tablename__ = "spotify_login"

    state_hash: Mapped[bytes] = mapped_column(LargeBinary, primary_key=True)
    tenant_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("tenant.id", ondelete="CASCADE"))
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("app_user.id", ondelete="CASCADE"))
    code_verifier: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
