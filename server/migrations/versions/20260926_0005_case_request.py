"""case_request: order requests for printed cases (docs/gehaeuse.md)

Revision ID: 0005
Revises: 0004
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

STATUS = ("unconfirmed", "confirmed", "answered", "done", "cancelled")


def upgrade() -> None:
    op.create_table(
        "case_request",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("email", sa.Text(), nullable=False),
        sa.Column("contact_name", sa.Text(), nullable=False),
        sa.Column("country", sa.Text(), nullable=False),
        sa.Column("quantity", sa.SmallInteger(), nullable=False),
        sa.Column("message", sa.Text(), server_default="", nullable=False),
        sa.Column("config", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("config_digest", sa.Text(), nullable=False),
        sa.Column("generator_version", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(*STATUS, name="case_request_status"),
            server_default="unconfirmed",
            nullable=False,
        ),
        sa.Column("token_hash", sa.LargeBinary(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_case_request")),
        sa.UniqueConstraint("token_hash", name=op.f("uq_case_request_token_hash")),
    )
    op.create_index("ix_case_request_status", "case_request", ["status"])


def downgrade() -> None:
    op.drop_index("ix_case_request_status", table_name="case_request")
    op.drop_table("case_request")
    sa.Enum(name="case_request_status").drop(op.get_bind(), checkfirst=False)
