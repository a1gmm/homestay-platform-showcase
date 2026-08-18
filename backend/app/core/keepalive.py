"""Neon serverless DB keepalive.

Neon free tier auto-suspends compute after 5 minutes of inactivity, causing
5-18s cold-start latency on the next request. We run a background task that
issues a lightweight `SELECT 1` every N seconds to keep compute warm.

Usage: see app.main lifespan — start a task with `start_keepalive(engine)`
and cancel it on shutdown.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine

logger = logging.getLogger(__name__)


async def keepalive_loop(engine: AsyncEngine, interval_seconds: int) -> None:
    """Loop forever, pinging the database every `interval_seconds`.

    Cancellation is propagated; transient ping failures are logged and ignored
    so a brief Neon hiccup doesn't kill the loop.
    """
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("DB keepalive ping failed: %s", exc)


def start_keepalive(engine: AsyncEngine, interval_seconds: int) -> Optional[asyncio.Task]:
    """Start the keepalive loop as a background task. Returns None if disabled."""
    if interval_seconds <= 0:
        return None
    return asyncio.create_task(keepalive_loop(engine, interval_seconds), name="db-keepalive")


async def stop_keepalive(task: Optional[asyncio.Task]) -> None:
    """Cancel a keepalive task started by `start_keepalive`. Safe to call with None."""
    if task is None:
        return
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
