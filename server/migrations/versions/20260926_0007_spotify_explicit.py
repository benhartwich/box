"""device_config.spotify_allow_explicit (SPEC v0.9 §3.4)

Revision ID: 0007
Revises: 0006
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "device_config",
        sa.Column(
            "spotify_allow_explicit", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )


def downgrade() -> None:
    op.drop_column("device_config", "spotify_allow_explicit")
