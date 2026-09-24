"""procrastinate application.

procrastinate has no asyncpg connector, so jobs use psycopg 3 while the web app uses asyncpg.
Tasks register on the module-level ``job_app``; the real connection is attached at runtime.
"""

from __future__ import annotations

import procrastinate

from box_server.settings import Settings

TASK_MODULES: list[str] = ["box_server.jobs.mail"]

job_app = procrastinate.App(connector=procrastinate.PsycopgConnector(), import_paths=TASK_MODULES)


def connected_job_app(settings: Settings) -> procrastinate.App:
    return job_app.with_connector(procrastinate.PsycopgConnector(conninfo=settings.database_url))
