"""Read-only daily reconciliation progress, separate from formal close approvals.

Existing ledger checks are reused. This projection cannot confirm a workflow
step, mark a source not applicable, approve a bill or register a payment.
"""
from __future__ import annotations

from typing import Any
from sqlalchemy import func, select, or_
from app.models.monthly_close import MonthlyCloseDocument
from app.models.financial_case import FinancialCaseSource
from app.models.settlement import OwnerSettlement, OwnerSettlementItem, SettlementStatus


async def current_bill_drift(db, cycle):
    from datetime import date
    from app.models.expense import Expense
    from app.api.v1.settlements import _build_settlement_financial_snapshot, _financial_snapshot_drift
    year, month = map(int, cycle.billing_month.split("-"))
    start, end = date(year, month, 1), date(year + (month == 12), month % 12 + 1, 1)
    bills = list(await db.scalars(select(OwnerSettlement).where(
        OwnerSettlement.billing_month == cycle.billing_month,
        OwnerSettlement.status == SettlementStatus.pending)))
    changed = 0
    for bill in bills:
        items = list(await db.scalars(select(OwnerSettlementItem).where(OwnerSettlementItem.settlement_id == bill.settlement_id)))
        current = await _build_settlement_financial_snapshot(db, bill.owner_id, year, month, for_update=False)
        fees = await db.scalars(select(Expense).where(Expense.owner_id == bill.owner_id,
            Expense.is_service_fee.is_(True), Expense.is_deleted.is_(False),
            Expense.expense_date >= start, Expense.expense_date < end))
        refs = {fee.expense_id: f"{fee.order_id}:{fee.room_id}:{fee.category.value}" for fee in fees}
        changed += _financial_snapshot_drift(bill, items, current, service_fee_refs=refs) is not None
    return changed


async def has_new_materials(db, cycle) -> bool:
    oldest_bill = await db.scalar(select(func.min(OwnerSettlement.created_at)).where(
        OwnerSettlement.billing_month == cycle.billing_month))
    if oldest_bill is None:
        return False
    document = await db.scalar(select(MonthlyCloseDocument.document_id).where(
        MonthlyCloseDocument.cycle_id == cycle.cycle_id,
        MonthlyCloseDocument.is_active.is_(True), MonthlyCloseDocument.uploaded_at > oldest_bill).limit(1))
    original = await db.scalar(select(FinancialCaseSource.source_id).outerjoin(
        MonthlyCloseDocument, MonthlyCloseDocument.document_id == FinancialCaseSource.document_id).where(
        FinancialCaseSource.cycle_id == cycle.cycle_id,
        FinancialCaseSource.kind.notin_(("feedback", "checklist")),
        or_(FinancialCaseSource.document_id.is_(None), MonthlyCloseDocument.is_active.is_(True)),
        FinancialCaseSource.created_at > oldest_bill).limit(1))
    return bool(document or original)


def business_progress(evidences, review, offline: dict[str, Any], *, new_materials=False, bill_drift_count=0) -> dict[str, Any]:
    preflight = next((row for row in evidences if row.step_key == "preflight"), None)
    check_count = preflight.blocking_count if preflight is not None else None
    disputed = sum(row.get("status") == "disputed" for row in review.settlements)
    return {
        "billing_check": "unavailable" if check_count is None else "needs_review" if check_count else "passed",
        "billing_issue_count": check_count,
        "offline_order_count": offline["count"],
        "offline_pending_count": offline["pending_count"],
        "offline_confirmed_count": offline["confirmed_count"],
        "offline_confirmed_amount": offline["confirmed_amount"],
        "disputed_bill_count": disputed,
        "new_materials_since_bills": new_materials,
        "bill_drift_count": bill_drift_count,
        "scope_note": "账单检查核对系统订单、平台差异和费用；人工确认实收与收款流水分开记录。资料是否齐全、公司银行收支及整月关账另行核实。",
    }


def prefer_bill_review(action, progress, review, cycle_status, cycle_id):
    """A saved bill is a real milestone; it never certifies every source as done.

    Preserve active safety/recovery and incoming-file work. A new source, failed
    analysis, or uncertain execution must not be hidden by an older bill.
    """
    if not progress or not review.settlement_count or cycle_status == "completed":
        return action
    if progress["new_materials_since_bills"]:
        return action
    if review.state in {"approved", "executing", "verifying", "succeeded_unverified", "unknown", "remediation"}:
        return action
    if action.kind not in {"review_work_log", "provide_source", "continue_analysis",
                           "confirm_workflow_step", "view_workflow_blocker"}:
        return action
    from app.services.monthly_close.projection import RecommendedAction
    if progress["billing_check"] != "passed":
        return RecommendedAction("view_workflow_blocker", "检查当前账单差异", "current_billing_checks",
                                 "workflow_step", "preflight")
    return RecommendedAction("review_owner_settlements", "复核业主账单与剩余事项", "owner_bills_available",
                             "cycle", cycle_id)
