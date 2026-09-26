"""MQTT accounts per box and remote commands (SPEC §6, M2)

Revision ID: 0010
Revises: 0009
Create Date: 2026-09-26
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0010"
down_revision: str | None = "0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("device", sa.Column("mqtt_password", sa.LargeBinary(), nullable=True))
    op.add_column(
        "device",
        sa.Column("mqtt_revoke", sa.Boolean(), server_default=sa.false(), nullable=False),
    )
    op.add_column("device", sa.Column("mqtt_online", sa.Boolean(), nullable=True))
    op.create_table(
        "device_command",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("device_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column(
            "args", postgresql.JSONB(astext_type=sa.Text()), server_default="{}", nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("result", sa.Text(), nullable=True),
        sa.Column("message", sa.Text(), nullable=True),
        sa.Column("acked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["device_id"], ["device.id"], name=op.f("fk_device_command_device_id_device"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenant.id"], name=op.f("fk_device_command_tenant_id_tenant"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_device_command")),
    )  # fmt: skip
    op.create_index(
        "ix_device_command_device_created", "device_command", ["device_id", "created_at"]
    )


def downgrade() -> None:
    op.drop_index("ix_device_command_device_created", table_name="device_command")
    op.drop_table("device_command")
    op.drop_column("device", "mqtt_online")
    op.drop_column("device", "mqtt_revoke")
    op.drop_column("device", "mqtt_password")
