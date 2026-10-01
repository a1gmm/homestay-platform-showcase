"""Explicit multi-month planning and transaction-scoped execution boundaries.

Preview functions are read-only. During confirmation, lock every selected month
(including the source workspace) first, verify the proposal snapshot, then call
ensure_month_cycles and persist all operations in the caller's ONE transaction.
Never call workflow.get_or_create_cycle here: it commits and releases locks.
"""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import and_, or_, select

from app.models.expense import Expense
from app.models.monthly_close import (
    MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES, MonthlyCloseCycle, MonthlyCloseSourceRequirement,
)
from app.models.settlement import OwnerSettlement, SettlementStatus
from app.services.audit import log_action_tx
from app.services.monthly_close.financial_lock import acquire_month_financial_lock
from app.services.monthly_close.workflow import MonthlyCloseConflict, validate_billing_month
from app.services.service_fee_ledger import lock_owner_service_fee_ledger


def normalize_months(months, default_month=None):
    """Stable, bounded explicit scope; an omitted scope means only the workspace."""
    if months is None or months == []:
        months = [default_month] if default_month is not None else []
    if not isinstance(months, (list, tuple)) or not months or len(months) > 120:
        raise ValueError('请选择 1 至 120 个明确的业务月份')
    if any(not isinstance(month, str) for month in months):
        raise ValueError('业务月份必须是 YYYY-MM')
    return sorted({validate_billing_month(month) for month in months})


def cycle_view(source_cycle, month):
    """Change eligibility month without changing case/source/idempotency identity."""
    month = normalize_months([month])[0]
    return SimpleNamespace(cycle_id=source_cycle.cycle_id, billing_month=month)


def operation_months(operations, allowed_months):
    """Reject date drift outside the concrete selection before any ledger writes."""
    allowed = set(normalize_months(allowed_months))
    months = set()
    for operation in operations:
        value = operation.get('expense_date')
        try:
            day = date.fromisoformat(value)
        except (TypeError, ValueError):
            raise ValueError('方案包含无效的费用日期') from None
        if day.isoformat() != value:
            raise ValueError('方案费用日期必须是 YYYY-MM-DD')
        month = value[:7]
        if month not in allowed or operation.get('business_month', month) != month:
            raise ValueError('方案费用超出已确认的业务月份，不能执行')
        months.add(month)
    return sorted(months)


async def load_month_ledgers(db, months):
    """Return only selected months, including deleted rows for stale-plan binding."""
    months = normalize_months(months)
    predicates = []
    for month in months:
        start = date.fromisoformat(month + '-01')
        end = date(start.year + int(start.month == 12), start.month % 12 + 1, 1)
        predicates.append(and_(Expense.expense_date >= start, Expense.expense_date < end))
    rows = list((await db.scalars(select(Expense).where(or_(*predicates))
        .order_by(Expense.expense_date, Expense.expense_id)
        .execution_options(populate_existing=True))).all())
    result = {month: [] for month in months}
    for row in rows:
        result[row.expense_date.strftime('%Y-%m')].append(row)
    return result


async def period_snapshot(db, months):
    """Bind scope, cycle state and frozen settlements without creating cycles."""
    months = normalize_months(months)
    cycles = {cycle.billing_month: cycle for cycle in (await db.scalars(
        select(MonthlyCloseCycle).where(MonthlyCloseCycle.billing_month.in_(months))
        .execution_options(populate_existing=True))).all()}
    frozen = {month: [] for month in months}
    for settlement in (await db.scalars(select(OwnerSettlement).where(
        OwnerSettlement.billing_month.in_(months),
        OwnerSettlement.status.in_([SettlementStatus.confirmed, SettlementStatus.paid]))
        .order_by(OwnerSettlement.settlement_id).execution_options(populate_existing=True))).all():
        frozen[settlement.billing_month].append(dict(id=settlement.settlement_id,
            owner_id=settlement.owner_id, status=settlement.status.value,
            updated_at=str(settlement.updated_at)))
    return [dict(billing_month=month,
        cycle_id=cycles[month].cycle_id if month in cycles else None,
        status=cycles[month].status if month in cycles else None,
        updated_at=str(cycles[month].updated_at) if month in cycles else None,
        frozen_settlements=frozen[month]) for month in months]


async def lock_writable_months(db, months, user_id=None, *, owner_ids=()):
    """Lock/check all months; never commit, create a cycle, or write an Expense.

    The caller must include the source workspace and run the normal live admin
    check, then re-read its proposal and compare its bound snapshots. Sorted month
    locks precede ALL cycle locks, owner locks, and settlement locks. Missing
    cycles are protected by the PostgreSQL advisory lock until transaction end.
    """
    months = normalize_months(months)
    for month in months:
        await acquire_month_financial_lock(db, month)
    cycles = {}
    for month in months:
        cycle = await db.scalar(select(MonthlyCloseCycle).where(MonthlyCloseCycle.billing_month == month)
            .with_for_update().execution_options(populate_existing=True))
        if cycle is not None:
            if cycle.status == 'completed':
                raise MonthlyCloseConflict('cycle_completed', f'{month} 月结已完成，请先按流程重新打开该月份')
            cycles[month] = cycle
    for owner in sorted(set(owner_ids)):
        await lock_owner_service_fee_ledger(db, owner)
    frozen = await db.scalar(select(OwnerSettlement).where(
        OwnerSettlement.billing_month.in_(months),
        OwnerSettlement.status.in_([SettlementStatus.confirmed, SettlementStatus.paid]))
        .order_by(OwnerSettlement.billing_month, OwnerSettlement.settlement_id)
        .with_for_update().execution_options(populate_existing=True).limit(1))
    if frozen is not None:
        raise MonthlyCloseConflict('settlement_locked',
            f'{frozen.billing_month} 有已确认或已支付的业主结算，请先处理该月结算锁定')
    return cycles


async def ensure_month_cycles(db, months, user_id):
    """Create missing workspaces AFTER snapshot validation under held month locks.

    This is the transaction-preserving counterpart of get_or_create_cycle. It
    intentionally does not commit or open a savepoint that could hide a failure.
    Its new cycles, requirements, audit and expenses roll back together.
    """
    result = {}
    for month in normalize_months(months):
        cycle = await db.scalar(select(MonthlyCloseCycle).where(MonthlyCloseCycle.billing_month == month)
            .with_for_update().execution_options(populate_existing=True))
        if cycle is not None and cycle.status == 'completed':
            raise MonthlyCloseConflict('cycle_completed', f'{month} 月结已完成，请先按流程重新打开该月份')
        if cycle is None:
            cycle = MonthlyCloseCycle(cycle_id='MCL-' + uuid4().hex[:12].upper(),
                                      billing_month=month, created_by=user_id)
            db.add(cycle)
            await db.flush()
            db.add_all([MonthlyCloseSourceRequirement(requirement_id='MCR-' + uuid4().hex[:12].upper(),
                cycle_id=cycle.cycle_id, source_type=source_type)
                for source_type in MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES])
            await log_action_tx(db, user_id, 'monthly_close.create', 'monthly_close', cycle.cycle_id,
                                after_data={'billing_month': month, 'step_count': 9})
            await db.flush()
        result[month] = cycle
    return result
