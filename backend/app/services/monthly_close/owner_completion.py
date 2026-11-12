"""Owner reconciliation completion; never closes company accounts or pays bills."""
from datetime import date, datetime, timezone
from hashlib import sha256
from fastapi import HTTPException
from sqlalchemy import select, func, or_
from app.models.monthly_close import MonthlyCloseCycle, MonthlyCloseDocument, MonthlyCloseInboxItem
from app.models.monthly_close_control import MonthlyCloseExecutionAttempt, MonthlyCloseRemediation
from app.models.financial_case import FinancialCaseSource
from app.models.settlement import OwnerSettlement, OwnerSettlementItem
from app.models.order import Order
from app.models.expense import Expense
from app.models.payment import Payment
from app.models.refund import Refund
from app.models.order_room import OrderRoom
from app.services.financial_case.service import require_admin
from app.services.monthly_close.review_queue import digest, build_review_queue
from app.services.monthly_close.settlement_recovery import compensated_attempt_exists
from app.services.audit import log_action_tx

SCOPE = "owner_reconciliation_v1"


def is_owner_completion(cycle):
    return isinstance(cycle.final_snapshot, dict) and cycle.final_snapshot.get("scope") == SCOPE


async def evidence_hash(db, cycle):
    """Hash business evidence without projecting the workflow (no recursion).

    Completion metadata is excluded. Payment registration on an owner bill is
    independent of reconciliation; its financial amounts remain in the hash.
    """
    year, month = map(int, cycle.billing_month.split("-"))
    start, end = date(year, month, 1), date(year + (month == 12), month % 12 + 1, 1)
    bills = select(OwnerSettlement.settlement_id).where(OwnerSettlement.billing_month == cycle.billing_month)
    monthly_legs = select(OrderRoom.order_id).where(OrderRoom.check_out_date >= start, OrderRoom.check_out_date < end)
    orders = select(Order.order_id).where(or_(
        (Order.check_out_date >= start) & (Order.check_out_date < end), Order.order_id.in_(monthly_legs)))
    specs = [
        (OwnerSettlement, OwnerSettlement.billing_month == cycle.billing_month, {"updated_at", "payment_date", "doc_url", "notes"}),
        (OwnerSettlementItem, OwnerSettlementItem.settlement_id.in_(bills), set()),
        (Order, Order.order_id.in_(orders), {"updated_at"}),
        (Expense, (Expense.expense_date >= start) & (Expense.expense_date < end) & Expense.owner_id.is_not(None), {"updated_at"}),
        (Payment, Payment.order_id.in_(orders), set()),
        (Refund, Refund.order_id.in_(orders), set()),
        (OrderRoom, OrderRoom.order_id.in_(orders), {"updated_at"}),
        (MonthlyCloseExecutionAttempt, MonthlyCloseExecutionAttempt.cycle_id == cycle.cycle_id, set()),
        (MonthlyCloseRemediation, MonthlyCloseRemediation.cycle_id == cycle.cycle_id, set()),
        (MonthlyCloseInboxItem, MonthlyCloseInboxItem.cycle_id == cycle.cycle_id, {"content"}),
        (FinancialCaseSource, (FinancialCaseSource.cycle_id == cycle.cycle_id) & FinancialCaseSource.kind.notin_(("feedback", "checklist")), {"content"}),
        (MonthlyCloseDocument, (MonthlyCloseDocument.cycle_id == cycle.cycle_id) & MonthlyCloseDocument.is_active.is_(True), {"content", "file_content"}),
    ]
    result = {}
    for model, condition, excluded in specs:
        columns = [c for c in model.__table__.columns if c.name not in excluded and str(c.type) != "BLOB" and str(c.type) != "BYTEA"]
        rows = [dict(r) for r in (await db.execute(select(*columns).where(condition).order_by(*model.__table__.primary_key.columns))).mappings()]
        if model is OwnerSettlement:
            for row in rows:
                if row["status"] in {"confirmed", "paid"}:
                    row["status"] = "confirmed"
        result[model.__tablename__] = digest(rows)
    # Hash actual bytes as well as metadata; a damaged attachment must make an
    # existing completion stale even if its stored digest was not changed.
    for model in (FinancialCaseSource, MonthlyCloseDocument):
        condition = model.cycle_id == cycle.cycle_id
        if model is FinancialCaseSource:
            condition = condition & model.kind.notin_(("feedback", "checklist"))
        else:
            condition = condition & model.is_active.is_(True)
        content_rows = await db.execute(select(*model.__table__.primary_key.columns, model.content).where(condition).order_by(*model.__table__.primary_key.columns))
        result[model.__tablename__ + "_bytes"] = digest([(row[0], sha256(row[1] or b"").hexdigest()) for row in content_rows])
    return digest(result)


async def is_fresh(db, cycle):
    return is_owner_completion(cycle) and cycle.final_snapshot.get("evidence_hash") == await evidence_hash(db, cycle)


async def preview(db, cycle, actor):
    try:
        await require_admin(db, actor)
    except PermissionError as exc:
        raise HTTPException(403, detail="仅管理员可以完成业主对账") from exc
    from app.services.monthly_close.workflow import build_role_cycle_projection
    projection = await build_role_cycle_projection(db, cycle, actor)
    queue = await build_review_queue(db, cycle, actor, projection=projection)
    review = projection["final_review"]
    blockers = [q["message"] for q in queue["questions"]]
    if not review["settlement_count"]:
        blockers.append("请先生成本月业主账单。")
    elif review["confirmed_settlement_count"] != review["settlement_count"]:
        blockers.append("请先确认全部业主账单。")
    sources = (projection.get("financial_case") or {}).get("sources", [])
    if not sources and not any(s.get("documents") for s in projection["sources"]):
        blockers.append("请先交本月原始资料并完成核对。")
    unsafe = await db.scalar(select(func.count()).select_from(MonthlyCloseExecutionAttempt).where(
        MonthlyCloseExecutionAttempt.cycle_id == cycle.cycle_id,
        MonthlyCloseExecutionAttempt.status.notin_(("verified", "failed_safe", "failed_confirmed")),
        ~compensated_attempt_exists()))
    remediation = await db.scalar(select(func.count()).select_from(MonthlyCloseRemediation).where(
        MonthlyCloseRemediation.cycle_id == cycle.cycle_id, MonthlyCloseRemediation.status != "resolved"))
    inbox = await db.scalar(select(func.count()).select_from(MonthlyCloseInboxItem).where(
        MonthlyCloseInboxItem.cycle_id == cycle.cycle_id,
        MonthlyCloseInboxItem.status.notin_(("confirmed", "dismissed"))))
    if unsafe or remediation:
        blockers.append("还有执行结果待核验或补救，请先处理，避免重复记账。")
    if inbox:
        blockers.append("还有新交资料未完成识别或分类，请先处理。")
    from app.services.monthly_close.task_delivery import verify_delivery
    delivery = await verify_delivery(db, cycle, actor)
    if delivery["status"] != "ready":
        blockers.extend(check["detail"] for check in delivery["checks"] if check["status"] == "blocked")
    current_hash = await evidence_hash(db, cycle)
    completed = not blockers and cycle.status == "completed" and is_owner_completion(cycle) and cycle.final_snapshot.get("evidence_hash") == current_hash
    if cycle.status == "completed" and not completed:
        blockers.append("归档后的依据已变化或属于历史关账，请先重新打开并核对。")
    return {"billing_month": cycle.billing_month, "cycle_id": cycle.cycle_id,
            "scope": SCOPE, "completed": completed, "ready": not blockers,
            "blockers": list(dict.fromkeys(blockers)), "evidence_hash": current_hash,
            "settlements": list(review["settlements"]), "total_amount": review["settlement_total_amount"]}


async def complete(db, cycle, actor, expected_hash):
    # All writes and the audit share one transaction. The standard cycle lock
    # also serializes source/assistant writes and repeated completion requests.
    try:
        await require_admin(db, actor, lock=True)
    except PermissionError as exc:
        raise HTTPException(403, detail="仅管理员可以完成业主对账") from exc
    locked = await db.scalar(select(MonthlyCloseCycle).where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
                             .with_for_update().execution_options(populate_existing=True))
    result = await preview(db, locked, actor)
    if result["completed"]:
        return result
    if not result["ready"]:
        raise HTTPException(409, detail={"message": "还有业主对账事项未完成", "blockers": result["blockers"]})
    if expected_hash != result["evidence_hash"]:
        raise HTTPException(409, detail="账单或依据已经变化，请重新检查后确认。")
    previous = locked.status
    locked.status = "completed"
    locked.completed_by = actor["user_id"] if isinstance(actor, dict) else actor.user_id
    locked.completed_at = datetime.now(timezone.utc)
    locked.final_snapshot = {**result, "completed": True, "completed_at": locked.completed_at.isoformat()}
    locked.monitor_effective_status = "completed"
    locked.monitor_current_step = None
    locked.monitor_progress = 9
    locked.monitor_blocking_count = 0
    await log_action_tx(db, locked.completed_by, "monthly_close.complete_owner_reconciliation", "monthly_close", locked.cycle_id,
                        before_data={"status": previous}, after_data=locked.final_snapshot)
    await db.commit()
    return {**result, "completed": True}
