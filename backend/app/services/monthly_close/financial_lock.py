"""One transaction-level lock boundary for monthly financial mutations.

Global order for participating paths is:
month advisory lock -> monthly-close cycle -> owner ledger -> settlement -> order.
PostgreSQL releases the advisory lock with the surrounding transaction.  SQLite
tests are single-connection and use a deterministic cycle-row touch as their
fallback; PostgreSQL concurrency tests are the authoritative contract.
"""
from __future__ import annotations

import re

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monthly_close import MonthlyCloseCycle


_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")


async def acquire_month_financial_lock(
    db: AsyncSession, billing_month: str
) -> None:
    if not _MONTH_RE.fullmatch(billing_month):
        raise ValueError("billing_month must be YYYY-MM")
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(
            text(
                "SELECT pg_advisory_xact_lock("
                "hashtext('monthly-close-financial'), hashtext(:billing_month))"
            ),
            {"billing_month": billing_month},
        )
        return
    await db.scalar(
        select(MonthlyCloseCycle.cycle_id)
        .where(MonthlyCloseCycle.billing_month == billing_month)
        .with_for_update()
    )
