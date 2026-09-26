"""pairing key per box; index for waiting commands (SPEC v0.12 §7.1, §6.2)

Revision ID: 0011
Revises: 0010
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0011"
down_revision: str | None = "0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("device", sa.Column("pairing_key_hash", sa.Text(), nullable=True))
    op.create_index(
        "ix_device_command_waiting",
        "device_command",
        ["created_at"],
        postgresql_where=sa.text("result IS NULL"),
    )


def downgrade() -> None:
    op.drop_index("ix_device_command_waiting", table_name="device_command")
    op.drop_column("device", "pairing_key_hash")
