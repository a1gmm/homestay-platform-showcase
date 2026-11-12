"""Reconcile automatic checkout fees when late continuation entry changes a stay.

Only deterministic, unpaid, system-generated fees may move. Original ledger rows
are retained as voided evidence. Confirmed statements and real cleaning receipts
are never rewritten by an order-creation confirmation.
"""
import re
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select

from app.models.cleaning_request import CleaningRequest
from app.models.expense import Expense, ExpenseCategory
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.settlement import OwnerSettlement, SettlementStatus
from app.models.task import Task, TaskType, TaskStatus
from app.services import service_fee_ledger
from app.services.audit import log_action_tx
from app.services.reconciliation_policy import service_cost_payer


async def continuation_fee_context(db, order, room, *, fail_fast=False):
    if not order or not order.stay_group_id:
        return None
    from app.services.stay_group import is_managed_split
    if await is_managed_split(db, order.stay_group_id):
        # Managed split stays have their own physical-leg and settlement rules.
        return None
    rows = (await db.execute(select(Order, OrderRoom).join(OrderRoom).where(
        Order.stay_group_id == order.stay_group_id, Order.is_deleted.is_(False),
        Order.order_status != OrderStatus.cancelled, OrderRoom.room_id == room.room_id,
    ).order_by(OrderRoom.check_in_date, OrderRoom.check_out_date, Order.order_id))).all()
    if len(rows) < 2:
        return None
    def reject():
        raise service_fee_ledger.CheckoutExpenseRecognitionError(
            reason="continuation_fee_review", order_id=order.order_id, room_ids=(room.room_id,))
    if len({service_cost_payer(o) for o, _ in rows}) != 1:
        reject()
    await service_fee_ledger.lock_owner_service_fee_ledger(db, room.owner_id, nowait=fail_fast)
    order_ids = [o.order_id for o, _ in rows]
    fees = (await db.execute(select(Expense).where(
        Expense.order_id.in_(order_ids), Expense.room_id == room.room_id,
        Expense.is_deleted.is_(False),
        service_fee_ledger.checkout_service_fee_identity_clause(),
    ).with_for_update(nowait=fail_fast))).scalars().all()
    final_order, final_room = rows[-1]
    earlier = [e for e in fees if e.order_id != final_order.order_id]
    total_nights = sum((r.check_out_date - r.check_in_date).days for _, r in rows)
    recorded_nights = sum(int(match[1]) for e in fees
        if e.category == ExpenseCategory.daily_supplies
        and (match := re.search(r"(?:^|\s)(\d+)晚", e.description)))
    has_daily = any(e.category == ExpenseCategory.daily_supplies for e in fees)
    tasks = (await db.execute(select(Task).where(
        Task.order_id.in_([o.order_id for o, _ in rows[:-1]]),
        Task.room_id == room.room_id, Task.task_type == TaskType.cleaning,
        Task.status != TaskStatus.cancelled,
    ))).scalars().all()
    if any(t.cleaning_started_at or t.submitted_at or t.completed_at
           or t.status in (TaskStatus.in_progress, TaskStatus.done) for t in tasks):
        reject()
    if not fees or (not earlier and (not has_daily or recorded_nights == total_nights)):
        return {"final": final_order, "rows": rows, "fees": fees, "earlier": []}
    if any(a.check_out_date != b.check_in_date for (_, a), (_, b) in zip(rows, rows[1:])):
        reject()
    if any(not e.is_service_fee or e.created_by or e.receipt_url or e.payment_date
           or e.paid_by or e.owner_id != room.owner_id
           or e.payer != service_cost_payer(final_order) for e in fees):
        reject()
    if await db.scalar(select(CleaningRequest.request_id).where(
        CleaningRequest.expense_id.in_([e.expense_id for e in fees])).limit(1)):
        reject()
    months = {e.expense_date.strftime("%Y-%m") for e in fees}
    months.add(final_room.check_out_date.strftime("%Y-%m"))
    if await db.scalar(select(OwnerSettlement.settlement_id).where(
        OwnerSettlement.owner_id == room.owner_id,
        OwnerSettlement.billing_month.in_(months),
        OwnerSettlement.status != SettlementStatus.pending,
    ).limit(1)):
        reject()
    # Missing or duplicate automatic rows can represent an explicit waiver or a
    # prior correction. Do not guess their basis from today's configured prices.
    keys = [(e.order_id, e.category) for e in fees]
    if len(keys) != len(set(keys)):
        reject()
    for oid in {e.order_id for e in fees}:
        if {e.category for e in fees if e.order_id == oid} != {
            ExpenseCategory.cleaning, ExpenseCategory.laundry, ExpenseCategory.daily_supplies}:
            reject()
    rates = set()
    covered = 0
    daily_total = Decimal("0")
    for e in fees:
        if e.category != ExpenseCategory.daily_supplies:
            continue
        match = re.search(r"(?:^|\s)(\d+)晚", e.description)
        if not match or int(match[1]) <= 0:
            reject()
        nights = int(match[1])
        covered += nights
        daily_total += e.amount
        rates.add(e.amount / nights)
    nights = total_nights
    if covered > nights or len(rates) != 1:
        reject()
    # Preserve the recorded historical basis for added/backfilled nights too.
    amounts = {ExpenseCategory.daily_supplies: daily_total + (nights - covered) * next(iter(rates))}
    for category in (ExpenseCategory.cleaning, ExpenseCategory.laundry):
        values = {e.amount for e in fees if e.category == category}
        if len(values) != 1:
            reject()
        amounts[category] = next(iter(values))
    return {"final": final_order, "rows": rows, "fees": fees, "earlier": fees,
            "amounts": amounts, "nights": nights, "date": final_room.check_out_date}


async def consolidate_continuation_fees(db, room, context, operator_id=None):
    """Called only once the final checkout is incurred; no independent commit."""
    if not context or not context["earlier"]:
        return
    from app.services.cleaning import _add_service_expense
    final = context["final"]
    now = datetime.now(timezone.utc)
    before = []
    for e in context["fees"]:
        before.append({"expense_id": e.expense_id, "order_id": e.order_id,
                       "amount": str(e.amount), "date": str(e.expense_date), "category": e.category.value})
        e.is_deleted, e.deleted_at, e.deleted_by = True, now, operator_id
    for category, amount in context["amounts"].items():
        if amount <= 0:
            continue
        prefix = service_fee_ledger.CHECKOUT_SERVICE_FEE_PREFIXES[category]
        description = f"{prefix} {room.room_name}"
        if category == ExpenseCategory.daily_supplies:
            description += f" {context['nights']}晚"
        _add_service_expense(db, room=room, order_id=final.order_id, category=category,
            amount=amount, description=description, expense_date=context["date"],
            payer=service_cost_payer(final))
    await log_action_tx(db, operator_id, "order.continuation_service_fees", "order", final.order_id,
        before_data={"fees": before}, after_data={"stay_group_id": final.stay_group_id,
            "room_id": room.room_id, "amounts": {k.value: str(v) for k, v in context["amounts"].items()}},
        notes="续住最终退房统一记费；原自动费用作废留痕，保留历史单价依据")
    await db.flush()
