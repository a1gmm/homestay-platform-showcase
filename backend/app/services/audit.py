"""
Audit log service — writes critical operations to audit_logs table.

Fire-and-forget: schedules the write on a fresh session so the caller's request
session is unaffected (no second commit on the response path). Failures are
logged but never propagated to the user-facing flow.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.models.audit_log import AuditLog

logger = logging.getLogger(__name__)

# Hold strong refs to in-flight tasks so they aren't GC'd mid-write.
_pending: set[asyncio.Task] = set()


async def log_action(
    db: AsyncSession,
    operator_id: Optional[str],
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    before_data: Optional[dict] = None,
    after_data: Optional[dict] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """
    Schedule an audit log write on a background task.

    Returns immediately — the actual DB write happens off the request hot path,
    so the response isn't blocked by an extra commit (was previously ~100-200ms
    on Neon serverless). The `db` parameter is retained for signature
    compatibility but is no longer used; the write opens its own session.
    """
    payload = dict(
        operator_id=operator_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before_data=before_data,
        after_data=after_data,
        ip_address=ip_address,
        user_agent=user_agent,
        notes=notes,
    )

    async def _write() -> None:
        try:
            async with AsyncSessionLocal() as session:
                session.add(AuditLog(**payload))
                await session.commit()
        except Exception:
            logger.exception("Failed to write audit log: action=%s", action)

    task = asyncio.create_task(_write())
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def log_action_tx(
    db: AsyncSession,
    operator_id: Optional[str],
    action: str,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    before_data: Optional[dict] = None,
    after_data: Optional[dict] = None,
    ip_address: Optional[str] = None,
    user_agent: Optional[str] = None,
    notes: Optional[str] = None,
) -> None:
    """
    Add the audit row to the caller's session so it commits/rolls back with the
    business change. Call BEFORE the caller's db.commit().

    Use this for money-touching or destructive operations: if the audit insert
    fails the whole transaction rolls back, so a record can never disappear
    without a matching audit_logs row. Shares the caller's single commit, so it
    doesn't reintroduce the extra round-trip that log_action() was built to avoid.
    """
    db.add(AuditLog(
        operator_id=operator_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        before_data=before_data,
        after_data=after_data,
        ip_address=ip_address,
        user_agent=user_agent,
        notes=notes,
    ))
