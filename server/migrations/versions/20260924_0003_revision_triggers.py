"""revision triggers (SPEC §5.1)

The single place where revisions are raised:

* ``tenant.config_rev``: +1 once per transaction and tenant on any INSERT/UPDATE/DELETE of
  token, content, content_item or binding.
* ``content.rev``: +1 once per transaction on any change of the content row or its items.
* ``device.device_rev``: +1 once per transaction on any change of the device's device_config.

"Once per transaction" is tracked by storing ``pg_current_xact_id()`` next to the counter.
The UPDATE on the tenant row also locks it until commit, so revisions of a tenant are assigned
in commit order.

Revision ID: 0003
Revises: 0002
Create Date: 2026-09-24
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONFIG_TABLES = ("token", "content", "content_item", "binding")


def upgrade() -> None:
    op.execute("ALTER TABLE tenant ADD COLUMN config_rev_xact xid8")
    op.execute("ALTER TABLE device ADD COLUMN device_rev_xact xid8")
    op.execute("ALTER TABLE content ADD COLUMN rev_xact xid8")

    op.execute(
        """
        CREATE FUNCTION myboxi_bump_tenant_config_rev(tid uuid) RETURNS void
        LANGUAGE sql AS $$
            UPDATE tenant
               SET config_rev = config_rev + 1, config_rev_xact = pg_current_xact_id()
             WHERE id = tid AND config_rev_xact IS DISTINCT FROM pg_current_xact_id();
        $$
        """
    )
    op.execute(
        """
        CREATE FUNCTION myboxi_config_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM myboxi_bump_tenant_config_rev(OLD.tenant_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM myboxi_bump_tenant_config_rev(NEW.tenant_id);
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    for table in CONFIG_TABLES:
        op.execute(
            f"""
            CREATE TRIGGER {table}_config_rev
            AFTER INSERT OR UPDATE OR DELETE ON {table}
            FOR EACH ROW EXECUTE FUNCTION myboxi_config_changed()
            """
        )

    # content.rev: a new row counts as the change of its creating transaction.
    op.execute(
        """
        CREATE FUNCTION myboxi_content_rev() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                NEW.rev_xact := pg_current_xact_id();
            ELSIF NEW.rev_xact IS DISTINCT FROM pg_current_xact_id() THEN
                NEW.rev := OLD.rev + 1;
                NEW.rev_xact := pg_current_xact_id();
            END IF;
            RETURN NEW;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER content_rev BEFORE INSERT OR UPDATE ON content
        FOR EACH ROW EXECUTE FUNCTION myboxi_content_rev()
        """
    )
    # Item changes touch the parent content row, which raises content.rev via content_rev.
    op.execute(
        """
        CREATE FUNCTION myboxi_content_item_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                UPDATE content SET updated_at = now()
                 WHERE id = OLD.content_id
                   AND rev_xact IS DISTINCT FROM pg_current_xact_id();
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                UPDATE content SET updated_at = now()
                 WHERE id = NEW.content_id
                   AND rev_xact IS DISTINCT FROM pg_current_xact_id();
            END IF;
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER content_item_content_rev
        AFTER INSERT OR UPDATE OR DELETE ON content_item
        FOR EACH ROW EXECUTE FUNCTION myboxi_content_item_changed()
        """
    )

    op.execute(
        """
        CREATE FUNCTION myboxi_device_config_changed() RETURNS trigger
        LANGUAGE plpgsql AS $$
        DECLARE did uuid;
        BEGIN
            IF TG_OP = 'DELETE' THEN did := OLD.device_id; ELSE did := NEW.device_id; END IF;
            UPDATE device
               SET device_rev = device_rev + 1, device_rev_xact = pg_current_xact_id()
             WHERE id = did AND device_rev_xact IS DISTINCT FROM pg_current_xact_id();
            RETURN NULL;
        END
        $$
        """
    )
    op.execute(
        """
        CREATE TRIGGER device_config_device_rev
        AFTER INSERT OR UPDATE OR DELETE ON device_config
        FOR EACH ROW EXECUTE FUNCTION myboxi_device_config_changed()
        """
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER device_config_device_rev ON device_config")
    op.execute("DROP FUNCTION myboxi_device_config_changed()")
    op.execute("DROP TRIGGER content_item_content_rev ON content_item")
    op.execute("DROP FUNCTION myboxi_content_item_changed()")
    op.execute("DROP TRIGGER content_rev ON content")
    op.execute("DROP FUNCTION myboxi_content_rev()")
    for table in CONFIG_TABLES:
        op.execute(f"DROP TRIGGER {table}_config_rev ON {table}")
    op.execute("DROP FUNCTION myboxi_config_changed()")
    op.execute("DROP FUNCTION myboxi_bump_tenant_config_rev(uuid)")
    op.execute("ALTER TABLE content DROP COLUMN rev_xact")
    op.execute("ALTER TABLE device DROP COLUMN device_rev_xact")
    op.execute("ALTER TABLE tenant DROP COLUMN config_rev_xact")
