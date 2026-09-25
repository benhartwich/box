"""resume_position.item_key (SPEC v0.8 §3.10)

Revision ID: 0006
Revises: 0005
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("resume_position", sa.Column("item_key", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("resume_position", "item_key")
