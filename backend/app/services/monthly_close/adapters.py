"""Read-only adapters from existing operational ledgers into monthly-close evidence."""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, time, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monthly_close import (
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseServiceLine,
    MonthlyCloseSourceRequirement,
)
from app.models.cleaning_request import (
    CleaningApprovalStatus,
    CleaningRequest,
    CleaningRequestStatus,
)
from app.models.order import OTA_PLATFORM_CHANNELS, Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.owner import Owner
from app.models.expense import Expense, ExpenseCategory
from app.models.recon import ReconBatch, ReconDiff, ReconDiffStatus
from app.models.settlement import OwnerSettlement, SettlementStatus
from app.models.utility_recon import (
    UtilityReconBatch,
    UtilityReconRow,
    UtilityReconSuggestion,
    UtilityReconUpload,
)
from app.core.datetime_helpers import CN_TZ, to_cn
from app.services.service_fee_reconciliation import plan_service_fee_reconciliation
from app.services.settlement_preflight import run_settlement_preflight


_OPEN_RECON_STATUSES = {ReconDiffStatus.pending, ReconDiffStatus.appeal_pending}
_CLEANING_EXPENSE_INTEGRITY_START = date(2026, 8, 1)


def _month_window(billing_month: str) -> tuple[date, date]:
    year, month = (int(part) for part in billing_month.split("-"))
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


def _issue(code: str, resource_id: str, message: str, **detail: Any) -> dict[str, Any]:
    return {
        "code": code,
        "resource_id": resource_id,
        "message": message,
        **detail,
    }


def _navigate(path: str, label: str, **query: str) -> dict[str, Any]:
    return {
        "kind": "navigate",
        "path": path,
        "label": label,
        "query": {key: value for key, value in query.items() if value},
    }


def _inline(path: str, label: str) -> dict[str, Any]:
    return {"kind": "inline", "path": path, "label": label, "query": {}}


def _snapshot(summary: dict[str, Any], issues: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "blocking_count": len(issues),
        "summary": summary,
        "issues": issues,
    }


async def order_integrity_snapshot(
    db: AsyncSession, cycle: MonthlyCloseCycle
) -> dict[str, Any]:
    start, end = _month_window(cycle.billing_month)
    rows = list(
        (
            await db.execute(
                select(Order, OrderRoom)
                .outerjoin(OrderRoom, OrderRoom.order_id == Order.order_id)
                .where(
                    Order.is_deleted.is_(False),
                    Order.order_status != OrderStatus.cancelled,
                    Order.check_out_date >= start,
                    Order.check_out_date < end,
                )
                .order_by(Order.order_id, OrderRoom.order_room_id.nulls_first())
            )
        ).all()
    )
    issues: list[dict[str, Any]] = []
    seen_order_issue: set[tuple[str, str]] = set()
    platform_ids: dict[tuple[str, str], set[str]] = defaultdict(set)
    for order, room in rows:
        if room is None:
            issues.append(
                _issue(
                    "missing_order_rooms",
                    order.order_id,
                    "订单没有任何房间明细。",
                    order_id=order.order_id,
                )
            )
        elif room.room_id is None:
            issues.append(
                _issue(
                    "missing_room",
                    room.order_room_id,
                    "订单仍有未排房明细。",
                    order_id=order.order_id,
                    order_room_id=room.order_room_id,
                )
            )
        if room is not None and room.check_out_date <= room.check_in_date:
            issues.append(
                _issue(
                    "invalid_stay_dates",
                    room.order_room_id,
                    "退房日期必须晚于入住日期。",
                    order_id=order.order_id,
                    order_room_id=room.order_room_id,
                )
            )
        if order.channel in OTA_PLATFORM_CHANNELS:
            if not (order.platform_order_id or "").strip():
                key = ("missing_platform_order_id", order.order_id)
                if key not in seen_order_issue:
                    seen_order_issue.add(key)
                    issues.append(
                        _issue(
                            key[0],
                            order.order_id,
                            "平台订单缺少平台订单号。",
                            order_id=order.order_id,
                        )
                    )
            else:
                channel = getattr(order.channel, "value", str(order.channel))
                platform_ids[(channel, order.platform_order_id.strip())].add(order.order_id)
    for (channel, platform_order_id), order_ids in sorted(platform_ids.items()):
        if len(order_ids) > 1:
            issues.append(
                _issue(
                    "duplicate_platform_order_id",
                    f"{channel}:{platform_order_id}",
                    "同一平台订单号关联了多张有效订单。",
                    platform=channel,
                    platform_order_id=platform_order_id,
                    order_ids=sorted(order_ids),
                )
            )
    for issue in issues:
        order_id = issue.get("order_id") or next(
            iter(issue.get("order_ids") or []), None
        )
        issue["action"] = _navigate(
            "/orders",
            "查看并修正订单",
            keyword=order_id or issue.get("platform_order_id") or "",
        )
    issues.sort(key=lambda item: (item["code"], item["resource_id"]))
    return _snapshot(
        {
            "order_count": len({order.order_id for order, _ in rows}),
            "order_room_count": sum(room is not None for _, room in rows),
            "issue_counts": dict(sorted(Counter(item["code"] for item in issues).items())),
        },
        issues,
    )


async def _requirements(
    db: AsyncSession, cycle_id: str
) -> dict[str, MonthlyCloseSourceRequirement]:
    rows = (
        await db.execute(
            select(MonthlyCloseSourceRequirement).where(
                MonthlyCloseSourceRequirement.cycle_id == cycle_id
            )
        )
    ).scalars()
    return {row.source_type: row for row in rows}


async def _active_documents(
    db: AsyncSession, cycle_id: str, source_types: set[str]
) -> list[MonthlyCloseDocument]:
    return list(
        (
            await db.execute(
                select(MonthlyCloseDocument).where(
                    MonthlyCloseDocument.cycle_id == cycle_id,
                    MonthlyCloseDocument.source_type.in_(source_types),
                    MonthlyCloseDocument.is_active.is_(True),
                )
            )
        ).scalars()
    )


async def service_fees_snapshot(
    db: AsyncSession, cycle: MonthlyCloseCycle
) -> dict[str, Any]:
    year, month = (int(part) for part in cycle.billing_month.split("-"))
    owner_ids = list(
        (
            await db.execute(select(Owner.owner_id).order_by(Owner.owner_id))
        ).scalars()
    )
    issues: list[dict[str, Any]] = []
    expected_count = 0
    missing_count = 0
    correction_count = 0
    for owner_id in owner_ids:
        plan = await plan_service_fee_reconciliation(db, owner_id, year, month)
        expected_count += len(plan.expected)
        missing_count += len(plan.missing)
        correction_count += len(plan.corrections)
        for item in plan.missing:
            issues.append(
                _issue(
                    "service_fee_missing",
                    f"{item.order_id}:{item.room_id}:{item.category.value}",
                    "系统服务费账本缺少应计费用。",
                    owner_id=owner_id,
                    order_id=item.order_id,
                    room_id=item.room_id,
                    service_type=item.category.value,
                    amount=str(item.amount),
                )
            )
        for item in plan.corrections:
            issues.append(
                _issue(
                    "service_fee_amount_mismatch",
                    item.expense_id,
                    "系统服务费金额或日期与订单不一致。",
                    owner_id=owner_id,
                    expense_id=item.expense_id,
                    expected_amount=str(item.expected.amount),
                    current_amount=str(item.current_amount),
                )
            )
        for item in plan.unresolved:
            resource_id = item.order_id or item.stay_group_id or owner_id
            issues.append(
                _issue(
                    "service_fee_unresolved",
                    resource_id,
                    "系统服务费来源无法安全判断。",
                    owner_id=owner_id,
                    reason=item.reason,
                    order_ids=list(item.order_ids),
                    room_ids=list(item.room_ids),
                )
            )

    start, end = _month_window(cycle.billing_month)
    integrity_start = max(start, _CLEANING_EXPENSE_INTEGRITY_START)
    start_at = datetime.combine(
        integrity_start, time.min, tzinfo=CN_TZ
    ).astimezone(timezone.utc)
    end_at = datetime.combine(end, time.min, tzinfo=CN_TZ).astimezone(timezone.utc)
    renewal_requests: list[CleaningRequest] = []
    if end > _CLEANING_EXPENSE_INTEGRITY_START:
        renewal_requests = list(
            (
                await db.execute(
                    select(CleaningRequest).where(
                        or_(
                            and_(
                                CleaningRequest.status
                                == CleaningRequestStatus.cleaned,
                                CleaningRequest.cleaned_at >= start_at,
                                CleaningRequest.cleaned_at < end_at,
                            ),
                            and_(
                                CleaningRequest.status
                                == CleaningRequestStatus.cleaned,
                                CleaningRequest.cleaned_at.is_(None),
                                CleaningRequest.request_date >= integrity_start,
                                CleaningRequest.request_date < end,
                            ),
                            and_(
                                CleaningRequest.status
                                != CleaningRequestStatus.cleaned,
                                CleaningRequest.request_date >= integrity_start,
                                CleaningRequest.request_date < end,
                            ),
                        )
                    )
                )
            ).scalars()
        )
    renewal_expense_ids = {
        request.expense_id for request in renewal_requests if request.expense_id
    }
    renewal_expenses = {}
    if renewal_expense_ids:
        renewal_expenses = {
            expense.expense_id: expense
            for expense in (
                await db.execute(
                    select(Expense).where(Expense.expense_id.in_(renewal_expense_ids))
                )
            ).scalars()
        }
    expense_use_count = Counter(
        request.expense_id for request in renewal_requests if request.expense_id
    )
    reported_shared_expenses: set[str] = set()
    renewal_completed_count = 0
    renewal_missing_count = 0
    renewal_pending_count = 0
    renewal_incomplete_count = 0
    for request in renewal_requests:
        completed = request.status == CleaningRequestStatus.cleaned
        approved = request.approval_status == CleaningApprovalStatus.approved
        if completed and not approved:
            renewal_pending_count += 1
            issues.append(
                _issue(
                    "renewal_cleaning_pending_approval",
                    request.request_id,
                    "续住保洁已完成但尚未审批，不能静默漏账。",
                    request_id=request.request_id,
                    order_id=request.order_id,
                    room_id=request.room_id,
                )
            )
        if approved and not completed:
            renewal_incomplete_count += 1
            issues.append(
                _issue(
                    "renewal_cleaning_not_completed",
                    request.request_id,
                    "续住保洁已审批但未确认打扫完成，不能作为预计支出入账。",
                    request_id=request.request_id,
                    order_id=request.order_id,
                    room_id=request.room_id,
                )
            )
            continue
        if not completed:
            continue

        renewal_completed_count += 1
        completed_cn = to_cn(request.cleaned_at)
        expected_expense_date = (
            completed_cn.date() if completed_cn is not None else request.request_date
        )
        expense = renewal_expenses.get(request.expense_id or "")
        if expense is None or expense.is_deleted:
            renewal_missing_count += 1
            issues.append(
                _issue(
                    "renewal_cleaning_expense_missing",
                    request.request_id,
                    "已完成且已审批的续住保洁缺少有效支出。",
                    request_id=request.request_id,
                    order_id=request.order_id,
                    room_id=request.room_id,
                )
            )
            continue
        if expense_use_count[expense.expense_id] > 1:
            if expense.expense_id not in reported_shared_expenses:
                reported_shared_expenses.add(expense.expense_id)
                issues.append(
                    _issue(
                        "renewal_cleaning_expense_shared",
                        expense.expense_id,
                        "多次续住保洁共用同一条支出，费用被少记。",
                        expense_id=expense.expense_id,
                        order_id=request.order_id,
                        room_id=request.room_id,
                        request_count=expense_use_count[expense.expense_id],
                    )
                )
            continue
        if expense.expense_date != expected_expense_date:
            issues.append(
                _issue(
                    "renewal_cleaning_expense_date_mismatch",
                    expense.expense_id,
                    "续住保洁支出日期与实际完成日期不一致。",
                    expense_id=expense.expense_id,
                    order_id=request.order_id,
                    room_id=request.room_id,
                    expected_date=expected_expense_date.isoformat(),
                    current_date=expense.expense_date.isoformat(),
                )
            )
        if not expense.is_service_fee:
            issues.append(
                _issue(
                    "renewal_cleaning_not_service_fee",
                    expense.expense_id,
                    "续住保洁支出没有纳入保洁服务费对账。",
                    expense_id=expense.expense_id,
                    order_id=request.order_id,
                    room_id=request.room_id,
                )
            )

    documents = await _active_documents(
        db, cycle.cycle_id, {"cleaning_statement", "linen_statement"}
    )
    document_ids = [document.document_id for document in documents]
    lines = []
    if document_ids:
        lines = list(
            (
                await db.execute(
                    select(MonthlyCloseServiceLine).where(
                        MonthlyCloseServiceLine.document_id.in_(document_ids)
                    )
                )
            ).scalars()
        )
    for document in documents:
        if document.processing_status != "processed":
            issues.append(
                _issue(
                    "service_statement_unprocessed",
                    document.document_id,
                    "保洁或布草表格尚未完成结构确认。",
                    document_id=document.document_id,
                    source_type=document.source_type,
                    processing_error=document.processing_error,
                )
            )
    for line in lines:
        if line.match_status != "matched":
            issues.append(
                _issue(
                    f"service_line_{line.match_status}",
                    line.line_id,
                    "供应商服务明细与系统服务费不一致。",
                    line_id=line.line_id,
                    document_id=line.document_id,
                    expense_id=line.matched_expense_id,
                    business_key=line.business_key,
                    amount=str(line.amount),
                    issue_code=line.issue_code,
                )
            )
    uploaded_source_types = {document.source_type for document in documents}
    expected_categories: set[ExpenseCategory] = set()
    if "cleaning_statement" in uploaded_source_types:
        expected_categories.add(ExpenseCategory.cleaning)
    if "linen_statement" in uploaded_source_types:
        expected_categories.add(ExpenseCategory.laundry)
    if expected_categories:
        system_expenses = list(
            (
                await db.execute(
                    select(Expense).where(
                        Expense.is_deleted.is_(False),
                        Expense.is_service_fee.is_(True),
                        Expense.category.in_(expected_categories),
                        Expense.expense_date >= start,
                        Expense.expense_date < end,
                    )
                )
            ).scalars()
        )
        matched_expense_ids = {
            line.matched_expense_id
            for line in lines
            if line.match_status == "matched" and line.matched_expense_id
        }
        for expense in system_expenses:
            if expense.expense_id not in matched_expense_ids:
                issues.append(
                    _issue(
                        "service_line_system_only",
                        expense.expense_id,
                        "系统服务费没有对应的供应商明细。",
                        expense_id=expense.expense_id,
                        order_id=expense.order_id,
                        room_id=expense.room_id,
                        amount=str(expense.amount),
                    )
                )
    for issue in issues:
        if issue["code"] == "service_statement_unprocessed":
            issue["action"] = _inline(
                "#service-statement-mapping", "确认供应商表格结构"
            )
        elif issue.get("expense_id"):
            issue["action"] = _navigate(
                "/finance",
                "在支出明细中处理",
                tab="expenses",
                month=cycle.billing_month,
                search=issue["expense_id"],
            )
        elif issue["code"].startswith("service_line_"):
            issue["action"] = _inline(
                "#service-statement-replacement", "归档并上传修正版"
            )
        elif issue.get("order_id") or issue.get("order_ids"):
            issue["action"] = _navigate(
                "/orders",
                "查看并修正订单",
                keyword=issue.get("order_id")
                or next(iter(issue.get("order_ids") or []), ""),
            )
        else:
            issue["action"] = _inline(
                "#service-fee-reconcile", "补齐并重新匹配服务费"
            )
    issues.sort(key=lambda item: (item["code"], item["resource_id"]))
    return _snapshot(
        {
            "owner_count": len(owner_ids),
            "expected_service_fee_count": expected_count,
            "missing_service_fee_count": missing_count,
            "correction_count": correction_count,
            "renewal_cleaning_completed_count": renewal_completed_count,
            "renewal_cleaning_missing_count": renewal_missing_count,
            "renewal_cleaning_pending_approval_count": renewal_pending_count,
            "renewal_cleaning_not_completed_count": renewal_incomplete_count,
            "vendor_document_count": len(documents),
            "vendor_line_count": len(lines),
            "matched_vendor_line_count": sum(line.match_status == "matched" for line in lines),
        },
        issues,
    )


async def utilities_snapshot(
    db: AsyncSession, cycle: MonthlyCloseCycle
) -> dict[str, Any]:
    requirements = await _requirements(db, cycle.cycle_id)
    utility_types = {"utility_receipt", "utility_expense"}
    utility_states = {
        key: requirements[key].state for key in utility_types if key in requirements
    }
    documents = await _active_documents(db, cycle.cycle_id, utility_types)
    operating_documents = await _active_documents(
        db, cycle.cycle_id, {"operating_expenses"}
    )
    issues: list[dict[str, Any]] = []
    batch_ids = sorted(
        {
            document.engine_id
            for document in documents
            if document.engine_type == "utility_recon" and document.engine_id
        }
    )
    if utility_states and all(state == "not_applicable" for state in utility_states.values()):
        batch_ids = []
    else:
        for document in documents:
            if document.engine_type != "utility_recon" or not document.engine_id:
                issues.append(
                    _issue(
                        "utility_document_unprocessed",
                        document.document_id,
                        "水电资料尚未生成对账批次。",
                        document_id=document.document_id,
                        source_type=document.source_type,
                    )
                )
        if documents and not batch_ids:
            pass
        elif not documents:
            issues.append(
                _issue(
                    "utility_batch_missing",
                    cycle.billing_month,
                    "当月没有可复核的水电对账批次。",
                )
            )
    batches: list[UtilityReconBatch] = []
    if batch_ids:
        batches = list(
            (
                await db.execute(
                    select(UtilityReconBatch).where(
                        UtilityReconBatch.batch_id.in_(batch_ids),
                        UtilityReconBatch.month == cycle.billing_month,
                    )
                )
            ).scalars()
        )
        found = {batch.batch_id for batch in batches}
        for missing_id in sorted(set(batch_ids) - found):
            issues.append(
                _issue(
                    "utility_batch_missing",
                    missing_id,
                    "归档文件关联的水电批次不存在。",
                )
            )
        for batch in batches:
            if batch.status != "closed":
                issues.append(
                    _issue(
                        "utility_batch_open",
                        batch.batch_id,
                        "水电对账批次尚未关闭。",
                        batch_id=batch.batch_id,
                    )
                )
        unparseable_rows = list(
            (
                await db.execute(
                    select(
                        UtilityReconRow.batch_id,
                        UtilityReconRow.row_id,
                    ).where(
                        UtilityReconRow.batch_id.in_(batch_ids),
                        UtilityReconRow.disposition == "unparseable",
                    )
                )
            ).tuples()
        )
        rows_by_batch: dict[str, list[str]] = defaultdict(list)
        for batch_id, row_id in unparseable_rows:
            rows_by_batch[batch_id].append(row_id)
        uploads = {
            upload.upload_id: upload
            for upload in (
                await db.execute(
                    select(UtilityReconUpload).where(
                        UtilityReconUpload.upload_id.in_(
                            {batch.upload_id for batch in batches}
                        )
                    )
                )
            ).scalars()
        }
        for batch in batches:
            row_ids = rows_by_batch.get(batch.batch_id, [])
            upload = uploads.get(batch.upload_id)
            preflight_count = (
                (upload.preflight_stats or {}).get("unparseable", 0)
                if upload is not None
                else 0
            )
            unparseable_count = max(
                len(row_ids),
                preflight_count if isinstance(preflight_count, int) else 0,
            )
            if unparseable_count == 0:
                continue
            issues.append(
                _issue(
                    "utility_rows_unparseable",
                    batch.batch_id,
                    f"水电原表仍有 {unparseable_count} 行无法解析。",
                    batch_id=batch.batch_id,
                    row_ids=sorted(row_ids)[:20],
                )
            )
        suggestions = list(
            (
                await db.execute(
                    select(UtilityReconSuggestion).where(
                        UtilityReconSuggestion.batch_id.in_(batch_ids),
                        UtilityReconSuggestion.status == "pending",
                    )
                )
            ).scalars()
        )
        for suggestion in suggestions:
            issues.append(
                _issue(
                    "utility_suggestion_pending",
                    suggestion.suggestion_id,
                    "水电对账仍有未决定的建议。",
                    batch_id=suggestion.batch_id,
                    suggestion_id=suggestion.suggestion_id,
                )
            )

    operating_state = requirements.get("operating_expenses")
    if operating_state is not None and operating_state.state == "uploaded":
        for document in operating_documents:
            if document.engine_type != "expense_import" or not document.engine_id:
                issues.append(
                    _issue(
                        "operating_expense_unimported",
                        document.document_id,
                        "运营支出文件尚未完成导入。",
                        document_id=document.document_id,
                    )
                )
            elif document.processing_status != "processed" or (
                (document.metadata_ or {}).get("import_result", {}).get("failed")
            ):
                issues.append(
                    _issue(
                        "operating_expense_import_failed",
                        document.document_id,
                        "运营支出文件仍有未导入行。",
                        document_id=document.document_id,
                        failed=(document.metadata_ or {})
                        .get("import_result", {})
                        .get("failed", []),
                    )
                )
    for issue in issues:
        if issue["code"].startswith("operating_expense_"):
            issue["action"] = _inline(
                "#operating-expenses", "归档并上传修正版"
            )
        else:
            issue["action"] = _navigate(
                "/finance/utility-recon",
                "处理水电对账",
                month=cycle.billing_month,
                batch=issue.get("batch_id", ""),
            )
    issues.sort(key=lambda item: (item["code"], item["resource_id"]))
    return _snapshot(
        {
            "utility_states": utility_states,
            "utility_document_count": len(documents),
            "batch_ids": sorted(batch.batch_id for batch in batches),
            "closed_batch_count": sum(batch.status == "closed" for batch in batches),
            "operating_expense_document_count": len(operating_documents),
            "operating_expense_import_count": sum(
                document.engine_type == "expense_import" and bool(document.engine_id)
                for document in operating_documents
            ),
        },
        issues,
    )


async def ota_statements_snapshot(
    db: AsyncSession, cycle: MonthlyCloseCycle
) -> dict[str, Any]:
    requirements = await _requirements(db, cycle.cycle_id)
    requirement = requirements.get("ota_statement")
    documents = await _active_documents(db, cycle.cycle_id, {"ota_statement"})
    issues: list[dict[str, Any]] = []
    batch_ids = sorted(
        {
            document.engine_id
            for document in documents
            if document.engine_type == "billing_recon" and document.engine_id
        }
    )
    if requirement is not None and requirement.state == "not_applicable":
        batch_ids = []
    else:
        for document in documents:
            if document.engine_type != "billing_recon" or not document.engine_id:
                issues.append(
                    _issue(
                        "ota_document_unprocessed",
                        document.document_id,
                        "OTA账单尚未生成对账批次。",
                        document_id=document.document_id,
                    )
                )
        if not documents:
            issues.append(
                _issue(
                    "ota_batch_missing",
                    cycle.billing_month,
                    "当月没有可复核的OTA账单批次。",
                )
            )
    batches: list[ReconBatch] = []
    if batch_ids:
        batches = list(
            (
                await db.execute(
                    select(ReconBatch).where(
                        ReconBatch.batch_id.in_(batch_ids),
                        ReconBatch.bill_month == cycle.billing_month,
                    )
                )
            ).scalars()
        )
        found = {batch.batch_id for batch in batches}
        for missing_id in sorted(set(batch_ids) - found):
            issues.append(
                _issue("ota_batch_missing", missing_id, "归档文件关联的OTA批次不存在。")
            )
        for batch in batches:
            mapping = batch.mapping or {}
            if batch.status != "parsed" or mapping.get("archived_at"):
                issues.append(
                    _issue(
                        "ota_batch_unavailable",
                        batch.batch_id,
                        "OTA账单批次已拒绝或归档。",
                        batch_id=batch.batch_id,
                    )
                )
            if not mapping.get("reviewed_at"):
                issues.append(
                    _issue(
                        "ota_batch_not_reviewed",
                        batch.batch_id,
                        "OTA账单批次尚未复核。",
                        batch_id=batch.batch_id,
                    )
                )
        diffs = list(
            (
                await db.execute(
                    select(ReconDiff).where(
                        ReconDiff.batch_id.in_(batch_ids),
                        ReconDiff.status.in_(_OPEN_RECON_STATUSES),
                    )
                )
            ).scalars()
        )
        for diff in diffs:
            issues.append(
                _issue(
                    "ota_diff_open",
                    diff.diff_id,
                    "OTA账单仍有未处理差异。",
                    batch_id=diff.batch_id,
                    diff_id=diff.diff_id,
                    order_id=diff.order_id,
                    amount=str(diff.bill_amount) if diff.bill_amount is not None else None,
                )
            )
    for issue in issues:
        issue["action"] = _navigate(
            "/finance/billing-recon",
            "处理OTA账单差异",
            month=cycle.billing_month,
            batch=issue.get("batch_id", ""),
            diff=issue.get("diff_id", ""),
        )
    issues.sort(key=lambda item: (item["code"], item["resource_id"]))
    return _snapshot(
        {
            "source_state": requirement.state if requirement is not None else "pending",
            "document_count": len(documents),
            "batch_ids": sorted(batch.batch_id for batch in batches),
            "reviewed_batch_count": sum(bool((batch.mapping or {}).get("reviewed_at")) for batch in batches),
        },
        issues,
    )


def exception_clearance_snapshot(
    *source_snapshots: dict[str, Any]
) -> dict[str, Any]:
    issues: list[dict[str, Any]] = []
    for source_step, snapshot in zip(
        ("order_integrity", "service_fees", "utilities", "ota_statements"),
        source_snapshots,
        strict=True,
    ):
        for issue in snapshot["issues"]:
            issues.append({**issue, "source_step": source_step})
    issues.sort(key=lambda item: (item["source_step"], item["code"], item["resource_id"]))
    return _snapshot(
        {
            "open_exception_count": len(issues),
            "counts_by_step": dict(sorted(Counter(item["source_step"] for item in issues).items())),
        },
        issues,
    )


async def preflight_snapshot(
    db: AsyncSession, cycle: MonthlyCloseCycle
) -> dict[str, Any]:
    year, month = (int(part) for part in cycle.billing_month.split("-"))
    report = await run_settlement_preflight(db, year, month)
    issues = [
        _issue(
            item.code,
            item.order_id
            or item.expense_id
            or item.recon_diff_id
            or item.platform_order_id
            or item.code,
            item.message,
            order_id=item.order_id,
            room_id=item.room_id,
            expense_id=item.expense_id,
            recon_diff_id=item.recon_diff_id,
            platform_order_id=item.platform_order_id,
            amount=str(item.amount) if item.amount is not None else None,
        )
        for item in report.issues
    ]
    recon_diff_ids = {
        issue["recon_diff_id"] for issue in issues if issue.get("recon_diff_id")
    }
    recon_batch_by_diff: dict[str, str] = {}
    if recon_diff_ids:
        recon_batch_by_diff = dict(
            (
                await db.execute(
                    select(ReconDiff.diff_id, ReconDiff.batch_id).where(
                        ReconDiff.diff_id.in_(recon_diff_ids)
                    )
                )
            ).all()
        )
    for issue in issues:
        if issue["code"] == "open_reconciliation":
            issue["action"] = _navigate(
                "/finance/billing-recon",
                "处理OTA账单差异",
                month=cycle.billing_month,
                batch=recon_batch_by_diff.get(issue.get("recon_diff_id", ""), ""),
                diff=issue.get("recon_diff_id", ""),
            )
        elif issue.get("expense_id"):
            issue["action"] = _navigate(
                "/finance",
                "在支出明细中处理",
                tab="expenses",
                month=cycle.billing_month,
                search=issue["expense_id"],
            )
        else:
            issue["action"] = _navigate(
                "/orders",
                "查看并修正订单",
                keyword=issue.get("order_id")
                or issue.get("platform_order_id")
                or "",
            )
    return _snapshot(
        {"blocking": report.blocking, "counts": report.counts},
        issues,
    )


async def settlement_review_snapshot(
    db: AsyncSession, cycle: MonthlyCloseCycle
) -> dict[str, Any]:
    settlements = list(
        (
            await db.execute(
                select(OwnerSettlement)
                .where(OwnerSettlement.billing_month == cycle.billing_month)
                .order_by(OwnerSettlement.settlement_id)
            )
        ).scalars()
    )
    issues: list[dict[str, Any]] = []
    if not settlements:
        issues.append(
            _issue(
                "settlements_missing",
                cycle.billing_month,
                "当月业主结算单尚未生成。",
            )
        )
    for settlement in settlements:
        if settlement.status == SettlementStatus.disputed:
            issues.append(
                _issue(
                    "settlement_disputed",
                    settlement.settlement_id,
                    "业主结算单存在异议。",
                    settlement_id=settlement.settlement_id,
                    owner_id=settlement.owner_id,
                )
            )
    for issue in issues:
        issue["action"] = _navigate(
            "/settlements",
            "查看并复核结算单",
            month=cycle.billing_month,
        )
    # Owner confirmation state is deliberately excluded from this snapshot.
    # A pending -> confirmed owner action must unlock step 9, not invalidate the
    # administrator's step-8 review of the immutable financial amounts.
    summary_rows = [
        {
            "settlement_id": item.settlement_id,
            "owner_id": item.owner_id,
            "total_net_revenue": str(Decimal(item.total_net_revenue or 0)),
            "owner_amount": str(Decimal(item.owner_amount or 0)),
            "deducted_expenses": str(Decimal(item.deducted_expenses or 0)),
            "actual_owner_amount": str(Decimal(item.actual_owner_amount or 0)),
            "disputed": item.status == SettlementStatus.disputed,
        }
        for item in settlements
    ]
    return _snapshot(
        {
            "settlement_count": len(settlements),
            "settlements": summary_rows,
        },
        issues,
    )


async def owner_confirmation_snapshot(
    db: AsyncSession, cycle: MonthlyCloseCycle
) -> dict[str, Any]:
    settlements = list(
        (
            await db.execute(
                select(OwnerSettlement)
                .where(OwnerSettlement.billing_month == cycle.billing_month)
                .order_by(OwnerSettlement.settlement_id)
            )
        ).scalars()
    )
    issues: list[dict[str, Any]] = []
    if not settlements:
        issues.append(
            _issue(
                "settlements_missing",
                cycle.billing_month,
                "当月没有可供业主确认的结算单。",
            )
        )
    confirmed_values = {SettlementStatus.confirmed, SettlementStatus.paid}
    for settlement in settlements:
        if settlement.status not in confirmed_values:
            issues.append(
                _issue(
                    "owner_confirmation_pending",
                    settlement.settlement_id,
                    "业主尚未确认结算单。",
                    settlement_id=settlement.settlement_id,
                    owner_id=settlement.owner_id,
                    status=getattr(settlement.status, "value", settlement.status),
                )
            )
    for issue in issues:
        issue["action"] = _navigate(
            "/settlements",
            "查看业主确认状态",
            month=cycle.billing_month,
        )
    return _snapshot(
        {
            "settlement_count": len(settlements),
            "confirmed_count": sum(item.status in confirmed_values for item in settlements),
            "settlements": [
                {
                    "settlement_id": item.settlement_id,
                    "owner_id": item.owner_id,
                    "status": getattr(item.status, "value", item.status),
                }
                for item in settlements
            ],
        },
        issues,
    )
