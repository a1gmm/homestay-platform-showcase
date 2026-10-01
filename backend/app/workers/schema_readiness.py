"""Wait for API-owned migrations before a newly deployed worker accepts jobs.

API and Celery deployments start independently. The evidence invalidation hook
requires the task table even for unrelated business jobs; checking once at worker
startup avoids both deployment races and a database probe on every ORM flush.
"""
from __future__ import annotations

import asyncio
import logging
import time

from celery.signals import worker_init
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)
SCHEMA_WAIT_SECONDS = 60


async def wait_for_task_schema(probe, *, timeout_seconds=SCHEMA_WAIT_SECONDS, poll_interval=2):
    """Readiness is bounded even when database connectivity or a query stalls."""
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        try:
            remaining = max(0.001, deadline - time.monotonic())
            if await asyncio.wait_for(probe(), timeout=min(5, remaining)):
                return True
        except Exception:
            # Connection details may contain credentials; only emit fixed text
            # after the bounded startup window has been exhausted.
            pass
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        await asyncio.sleep(min(poll_interval, remaining))
    return False


async def _schema_ready():
    from app.core.config import settings
    from app.core.database import database_connect_args

    # This engine and loop are disposed before prefork starts; no live database
    # connection or asyncio driver resource is inherited by worker children.
    engine = create_async_engine(settings.DATABASE_URL, poolclass=NullPool,
        connect_args=database_connect_args(settings.DATABASE_URL, settings.APP_ENV),
        echo=False, hide_parameters=True)
    try:
        async def probe():
            async with engine.connect() as connection:
                return bool(await connection.scalar(text(
                    "SELECT to_regclass('public.monthly_close_tasks') IS NOT NULL"
                )))
        return await wait_for_task_schema(probe)
    finally:
        await engine.dispose()


def ensure_monthly_close_schema(sender=None, **kwargs):
    if getattr(getattr(sender, "app", None), "main", None) != "homestay":
        return
    try:
        ready = asyncio.run(_schema_ready())
    except Exception:
        ready = False
    if not ready:
        logger.error("Required monthly-close task schema was unavailable during worker startup; exiting for deployment retry.")
        # Celery Signal.send catches Exception, so a normal RuntimeError would
        # be logged and ignored. SystemExit is deliberately nonzero and escapes
        # that handler, preventing this process from receiving business jobs.
        raise SystemExit(75)


def register_worker_schema_guard():
    worker_init.connect(ensure_monthly_close_schema, weak=False,
                        dispatch_uid="homestay-monthly-close-schema-readiness")
