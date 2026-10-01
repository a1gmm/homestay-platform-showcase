"""Evidence-based task lifecycle and workload summaries; never commits for callers.

Date-only obligations are due at the end of their business day in Shanghai.
Unknown maintenance deadlines remain unknown, and cleaning requires inspection.
"""
from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal
import hashlib
import json

from sqlalchemy import and_, case, func, or_, select

from app.core.datetime_helpers import CN_TZ
from app.models.order import DepositStatus, Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.audit_log import AuditLog
from app.models.refund import Refund, RefundReason
from app.models.payment import Payment
from app.models.task import Task, TaskStatus, TaskType
from app.services.audit import log_action_tx

CN = CN_TZ
ACTIVE_STATUSES = (TaskStatus.pending, TaskStatus.in_progress)


def business_day_deadline(day: date) -> datetime:
    return datetime.combine(day, time(23, 59, 59), tzinfo=CN)


def task_attention_conditions(now: datetime):
    active = Task.status.in_(ACTIVE_STATUSES)
    return {
        "active": active,
        "overdue": and_(active, Task.deadline < now),
        "no_deadline": and_(active, Task.deadline.is_(None)),
        "unassigned": and_(active, Task.assignee_id.is_(None)),
        "aged": and_(active, Task.created_at <= now - timedelta(days=7)),
        "needs_attention": and_(active, or_(
            Task.deadline < now, Task.deadline.is_(None), Task.assignee_id.is_(None),
            Task.created_at <= now - timedelta(days=7),
        )),
    }


async def task_attention_summary(db, *, now=None, conditions=()) -> dict[str, int]:
    expressions = task_attention_conditions(now or datetime.now(timezone.utc))
    row = (await db.execute(select(*[
        func.coalesce(func.sum(case((expr, 1), else_=0)), 0).label(name)
        for name, expr in expressions.items()
    ]).where(*conditions))).one()
    return {name: int(getattr(row, name)) for name in expressions}


def _snapshot(task: Task) -> dict:
    return {"status": task.status.value, "deadline": task.deadline.isoformat() if task.deadline else None,
            "completed_at": task.completed_at.isoformat() if task.completed_at else None}


def _money_evidence(value):
    return str(Decimal(value).quantize(Decimal("0.01"))) if value is not None else None


async def _batch_evidence(db, tasks, orders):
    """Load shared financial/checkout evidence once per batch, not per task."""
    holders = {}
    groups = {}
    deposit_order_ids = {t.order_id for t in tasks if t.task_type in (TaskType.collect_deposit, TaskType.return_deposit)}
    for order in orders.values():
        holder = order
        if order.order_id in deposit_order_ids and order.stay_group_id and not order.is_deleted and order.order_status != OrderStatus.cancelled:
            from app.services.stay_group import deposit_holder
            if order.stay_group_id not in groups:
                groups[order.stay_group_id] = await deposit_holder(db, order)
            holder = groups[order.stay_group_id]
        holders[order.order_id] = holder
    holder_ids = {holders[oid].order_id for oid in deposit_order_ids if oid in holders}
    paid = dict((await db.execute(select(Payment.order_id, func.sum(Payment.amount)).where(
        Payment.order_id.in_(holder_ids), Payment.is_deleted.is_(False), Payment.is_deposit.is_(True),
    ).group_by(Payment.order_id))).all()) if holder_ids else {}
    refunded = dict((await db.execute(select(Refund.order_id, func.sum(Refund.amount)).where(
        Refund.order_id.in_(holder_ids), Refund.is_deleted.is_(False), Refund.reason == RefundReason.deposit_return,
    ).group_by(Refund.order_id))).all()) if holder_ids else {}
    done_ids = [t.task_id for t in tasks if t.status == TaskStatus.done]
    closure_reasons = {}
    if done_ids:
        latest = select(func.max(AuditLog.log_id)).where(
            AuditLog.resource_type == "task", AuditLog.resource_id.in_(done_ids),
            AuditLog.action == "task.reconcile",
        ).group_by(AuditLog.resource_id)
        closure_reasons = dict((await db.execute(select(AuditLog.resource_id, AuditLog.notes)
            .where(AuditLog.log_id.in_(latest)))).all())
    cleaning_orders = {t.order_id for t in tasks if t.task_type == TaskType.cleaning and t.deadline is None and t.order_id}
    checkouts = {}
    if cleaning_orders:
        checkouts = {(oid, rid): value for oid, rid, value in (await db.execute(select(
            OrderRoom.order_id, OrderRoom.room_id, func.max(OrderRoom.checked_out_at),
        ).where(OrderRoom.order_id.in_(cleaning_orders), OrderRoom.checked_out_at.is_not(None))
          .group_by(OrderRoom.order_id, OrderRoom.room_id))).all()}
    return dict(holders=holders, paid=paid, refunded=refunded, closure_reasons=closure_reasons, checkouts=checkouts)


async def _plan_task(db, task: Task, order: Order | None, *, backfill_deadlines: bool, context: dict) -> dict:
    changes: dict = {}
    reason = "manual_review_required"
    evidence: dict = {}
    payment_closed = False
    if task.status == TaskStatus.done:
        payment_closed = context["closure_reasons"].get(task.task_id) == "deposit_payments_confirmed"
        if not payment_closed:
            return {"task_id": task.task_id, "before": _snapshot(task), "changes": {},
                    "reason": "terminal_task_preserved", "evidence": {}}
    if order:
        evidence = {"order_id": order.order_id, "order_status": order.order_status.value,
                    "deposit_status": order.deposit_status.value, "is_deleted": order.is_deleted}
        holder = context["holders"][order.order_id]
        if holder is not order:
            evidence.update(deposit_holder=holder.order_id, deposit_status=holder.deposit_status.value)
        if task.task_type == TaskType.collect_deposit:
            paid = context["paid"].get(holder.order_id, Decimal("0"))
            refunded = context["refunded"].get(holder.order_id, Decimal("0"))
            evidence.update(required_deposit=_money_evidence(holder.deposit), deposit_paid=_money_evidence(paid),
                            deposit_refunded=_money_evidence(refunded))
            if holder.deposit_status in (DepositStatus.collected, DepositStatus.returned, DepositStatus.withheld):
                changes["status"] = TaskStatus.done.value
                reason = "deposit_collection_confirmed"
            elif order.is_deleted or order.order_status == OrderStatus.cancelled:
                changes["status"] = TaskStatus.cancelled.value
                reason = "order_cancelled_or_deleted"
            elif holder.deposit is not None and holder.deposit <= Decimal("0"):
                changes["status"] = TaskStatus.cancelled.value
                reason = "no_deposit_required"
            elif holder.deposit and paid - refunded >= holder.deposit:
                changes["status"] = TaskStatus.done.value
                reason = "deposit_payments_confirmed"
            elif payment_closed:
                changes["status"] = TaskStatus.pending.value
                reason = "deposit_payment_evidence_withdrawn"
            elif backfill_deadlines and task.deadline is None and order.check_in_date:
                changes["deadline"] = business_day_deadline(order.check_in_date).isoformat()
                reason = "deposit_due_on_checkin_day"
        elif task.task_type == TaskType.return_deposit and holder.deposit_status in (
            DepositStatus.returned, DepositStatus.withheld,
        ):
            changes["status"] = TaskStatus.done.value
            reason = "deposit_settlement_confirmed"
        elif backfill_deadlines and task.task_type == TaskType.cleaning and task.deadline is None:
            # Only an actual checkout on this task's creation day can date an old
            # cleaning obligation. Planned dates and later stays are not evidence.
            checkout = context["checkouts"].get((order.order_id, task.room_id))
            if checkout and task.created_at:
                checkout = checkout.replace(tzinfo=timezone.utc) if checkout.tzinfo is None else checkout
                created = task.created_at.replace(tzinfo=timezone.utc) if task.created_at.tzinfo is None else task.created_at
                if checkout.astimezone(CN).date() == created.astimezone(CN).date():
                    changes["deadline"] = business_day_deadline(checkout.astimezone(CN).date()).isoformat()
                    reason = "cleaning_due_on_actual_checkout_day"
                    evidence["checked_out_at"] = checkout.isoformat()
    if changes.get("status") == task.status.value:
        del changes["status"]
    return {"task_id": task.task_id, "before": _snapshot(task), "changes": changes,
            "reason": reason, "evidence": evidence}


async def _apply_plan(db, task: Task, plan: dict, operator_id: str | None, now: datetime):
    changes = plan["changes"]
    if not changes:
        return
    if "status" in changes:
        task.status = TaskStatus(changes["status"])
        # This is the time the evidence was reconciled, not an invented historical
        # payment timestamp. The audit records the evidence and reconciliation time.
        if task.status == TaskStatus.done:
            task.completed_at = now
        else:
            task.completed_at = None
    if "deadline" in changes:
        task.deadline = datetime.fromisoformat(changes["deadline"])
    await log_action_tx(db, operator_id, "task.reconcile", "task", task.task_id,
                        before_data=plan["before"], after_data={**_snapshot(task), "evidence": plan["evidence"]},
                        notes=plan["reason"])


async def sync_order_tasks(db, order: Order, operator_id: str | None = None) -> int:
    """Close deposit tasks when a recorded order/deposit transition proves it.

    Cleaning, inspection and custom repair obligations survive cancellation until
    staff confirm their disposition. Shared deposits are resolved by the holder.
    A missing operator is a system action, not a synthetic users-table identity.
    """
    order_ids = [order.order_id]
    if order.stay_group_id and not order.is_deleted and order.order_status != OrderStatus.cancelled:
        order_ids = list((await db.execute(select(Order.order_id).where(
            Order.stay_group_id == order.stay_group_id,
        ))).scalars())
    tasks = (await db.execute(select(Task).where(
        Task.order_id.in_(order_ids), _reconcilable_condition(),
        Task.task_type.in_((TaskType.collect_deposit, TaskType.return_deposit)),
    ).with_for_update())).scalars().all()
    now = datetime.now(timezone.utc)
    changed = 0
    orders = {o.order_id: o for o in (await db.execute(select(Order).where(Order.order_id.in_(order_ids)))).scalars()}
    orders[order.order_id] = order
    context = await _batch_evidence(db, tasks, orders)
    for task in tasks:
        plan = await _plan_task(db, task, orders.get(task.order_id), backfill_deadlines=False, context=context)
        if plan["changes"]:
            await _apply_plan(db, task, plan, operator_id, now)
            changed += 1
    return changed


async def reconcile_task_batch(db, *, task_ids: list[str] | None = None,
                               limit: int = 100, after_id: str | None = None,
                               apply: bool = False, expected_fingerprint: str | None = None,
                               operator_id: str | None = None) -> dict:
    """Preview at most 200 tasks; applying requires explicit IDs and preview hash.

    No commit here. An already-reconciled batch returns zero changes on replay.
    Callers must commit changes and audits together. Raises ValueError on bad scope
    and RuntimeError when the evidence changed since preview.
    """
    if not 1 <= limit <= 200 or (task_ids is not None and not 1 <= len(task_ids) <= 200):
        raise ValueError("每批必须为 1 至 200 项")
    if apply and (not task_ids or not expected_fingerprint):
        raise ValueError("执行需要明确任务 ID 和预览指纹")
    conditions = [_reconcilable_condition()]
    if task_ids is not None:
        conditions.append(Task.task_id.in_(task_ids))
    elif after_id:
        conditions.append(Task.task_id > after_id)
    if apply:
        order_ids = select(Task.order_id).where(*conditions)
        from sqlalchemy.orm import aliased
        member = aliased(Order)
        group_ids = select(member.stay_group_id).where(member.order_id.in_(order_ids), member.stay_group_id.is_not(None))
        # Shared-deposit holders can be a different group member. Lock every
        # member whose current evidence could authorize this batch, in one order.
        (await db.execute(select(Order).where(or_(Order.order_id.in_(order_ids), Order.stay_group_id.in_(group_ids)))
            .order_by(Order.order_id).with_for_update().execution_options(populate_existing=True))).scalars().all()
    q = select(Task).where(*conditions).order_by(Task.task_id).limit(len(task_ids) if task_ids else limit)
    if apply:
        q = q.with_for_update().execution_options(populate_existing=True)
    tasks = (await db.execute(q)).scalars().all()
    target_order_ids = {t.order_id for t in tasks if t.order_id}
    orders = {o.order_id: o for o in (await db.execute(select(Order).where(Order.order_id.in_(target_order_ids)))).scalars()} if target_order_ids else {}
    context = await _batch_evidence(db, tasks, orders)
    plans = [await _plan_task(db, task, orders.get(task.order_id),
                              backfill_deadlines=True, context=context) for task in tasks]
    fingerprint = hashlib.sha256(json.dumps(plans, sort_keys=True, default=str).encode()).hexdigest()
    changed = [p for p in plans if p["changes"]]
    if apply and changed and fingerprint != expected_fingerprint:
        raise RuntimeError("任务或业务证据已变化，请重新预览")
    if apply:
        now = datetime.now(timezone.utc)
        for task, plan in zip(tasks, plans):
            await _apply_plan(db, task, plan, operator_id, now)
    return {"dry_run": not apply, "scanned": len(tasks), "change_count": len(changed),
            "applied": len(changed) if apply else 0, "fingerprint": fingerprint,
            "next_after_id": tasks[-1].task_id if tasks else None, "items": plans}


def _reconcilable_condition():
    # A correction to a recorded payment can reopen only tasks this lifecycle
    # service previously closed using payment evidence, not manually closed work.
    payment_closure = select(AuditLog.log_id).where(
        AuditLog.resource_type == "task", AuditLog.resource_id == Task.task_id,
        AuditLog.action == "task.reconcile", AuditLog.notes == "deposit_payments_confirmed",
    ).correlate(Task).exists()
    return or_(Task.status.in_(ACTIVE_STATUSES), and_(
        Task.status == TaskStatus.done, Task.task_type == TaskType.collect_deposit, payment_closure,
    ))
