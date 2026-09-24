"""procrastinate application.

procrastinate has no asyncpg connector, so jobs use psycopg 3 while the web app uses asyncpg.
Tasks register on the single module-level ``job_app``; each process (web, worker) attaches
its real connection with ``open_job_app`` for its lifetime.
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncGenerator

import procrastinate

from box_server.settings import Settings

TASK_MODULES: list[str] = [
    "box_server.jobs.mail",
    "box_server.jobs.cleanup",
    "box_server.jobs.transcode",
]

job_app = procrastinate.App(connector=procrastinate.PsycopgConnector(), import_paths=TASK_MODULES)


@contextlib.asynccontextmanager
async def open_job_app(settings: Settings) -> AsyncGenerator[procrastinate.App]:
    connector = procrastinate.PsycopgConnector(conninfo=settings.database_url)
    with job_app.replace_connector(connector):
        async with job_app.open_async():
            yield job_app
