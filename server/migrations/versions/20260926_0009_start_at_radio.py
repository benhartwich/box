"""binding.start_at; radio on by default (SPEC v0.11 §3.9, §3.4)

Revision ID: 0009
Revises: 0008
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0009"
down_revision: str | None = "0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "ARRAY['local','podcast']::text[]"
NEW = "ARRAY['local','podcast','stream']::text[]"


def upgrade() -> None:
    op.add_column(
        "binding",
        sa.Column("start_at", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.alter_column("device_config", "providers_enabled", server_default=sa.text(NEW))
    # Boxes still on the old default get radio too; a deliberate choice stays untouched.
    op.execute(
        f"UPDATE device_config SET providers_enabled = {NEW} WHERE providers_enabled = {OLD}"  # noqa: S608
    )


def downgrade() -> None:
    op.alter_column("device_config", "providers_enabled", server_default=sa.text(OLD))
    op.drop_column("binding", "start_at")
