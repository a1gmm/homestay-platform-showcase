"""
Reusable async helper for Celery tasks.

Celery tasks are synchronous, but our DB layer (SQLAlchemy async) requires
an event loop. This module provides a single shared event loop that is
reused across all task invocations within a worker process, avoiding the
overhead of creating and tearing down a new loop per task.
"""
import asyncio
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")

_loop: asyncio.AbstractEventLoop | None = None


def _get_loop() -> asyncio.AbstractEventLoop:
    """Return a persistent event loop for the current worker process."""
    global _loop
    if _loop is None or _loop.is_closed():
        _loop = asyncio.new_event_loop()
        asyncio.set_event_loop(_loop)
    return _loop


def run_async(coro: Coroutine[Any, Any, T]) -> T:
    """
    Run an async coroutine from a synchronous Celery task.

    Uses a shared event loop that persists for the lifetime of the
    worker process, avoiding repeated loop creation/teardown overhead.
    """
    loop = _get_loop()
    task = loop.create_task(coro)
    try:
        return loop.run_until_complete(task)
    except BaseException:
        if not task.done():
            task.cancel()
        try:
            loop.run_until_complete(task)
        except BaseException:
            # Preserve the interruption that reached the synchronous Celery
            # task. The drain exists only to finish cancellation on this loop.
            pass
        raise
