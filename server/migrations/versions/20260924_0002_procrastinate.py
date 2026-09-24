"""procrastinate 3.10.0 job queue schema

The SQL is vendored from the pinned procrastinate release (server/pyproject.toml). When
upgrading procrastinate, add a new revision that applies the release's files from
``procrastinate/sql/migrations`` in order.

Revision ID: 0002
Revises: 0001
Create Date: 2026-09-24
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from alembic import op

revision: str = "0002"
down_revision: str | None = "0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA_SQL = Path(__file__).resolve().parents[1] / "sql" / "procrastinate_3.10.0_schema.sql"


def _execute_script(sql: str) -> None:
    # The file contains many statements and dollar-quoted function bodies; asyncpg only runs
    # multi-statement scripts through its simple query protocol, i.e. Connection.execute().
    dbapi_conn: Any = op.get_bind().connection.dbapi_connection
    dbapi_conn.run_async(lambda conn: conn.execute(sql))


def upgrade() -> None:
    _execute_script(SCHEMA_SQL.read_text())


def downgrade() -> None:
    _execute_script(
        """
        DROP TABLE IF EXISTS procrastinate_events, procrastinate_periodic_defers,
            procrastinate_jobs, procrastinate_workers CASCADE;
        DO $$
        DECLARE r record;
        BEGIN
            FOR r IN SELECT p.oid::regprocedure AS sig FROM pg_proc p
                     WHERE p.proname LIKE 'procrastinate%'
                       AND p.pronamespace = current_schema()::regnamespace
            LOOP
                EXECUTE 'DROP ROUTINE IF EXISTS ' || r.sig || ' CASCADE';
            END LOOP;
            FOR r IN SELECT t.typname FROM pg_type t
                     LEFT JOIN pg_class c ON c.oid = t.typrelid
                     WHERE t.typname LIKE 'procrastinate%'
                       AND t.typnamespace = current_schema()::regnamespace
                       AND (t.typtype = 'e' OR (t.typtype = 'c' AND c.relkind = 'c'))
            LOOP
                EXECUTE format('DROP TYPE IF EXISTS %I CASCADE', r.typname);
            END LOOP;
        END $$;
        """
    )
