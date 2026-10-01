"""Read-only adapters from existing operational ledgers into monthly-close evidence."""

from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from collections.abc import Sequence
from dataclasses import replace
from datetime import date, datetime, time, timezone
from decimal import Decimal
import hashlib
import json
import re
from typing import Any, Literal
from uuid import uuid4

from sqlalchemy import and_, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models.audit_log import AuditLog
from app.models.monthly_close import (
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseSourceRequirement,
    uses_legacy_utility_contract,
)
from app.models.monthly_close_control import (
    MonthlyCloseExecutionAttempt,
    MonthlyCloseIssueInstance,
    MonthlyCloseProposal,
    MonthlyCloseRemediation,
    MonthlyCloseVerification,
)
from app.models.cleaning_request import (
    CleaningApprovalStatus,
    CleaningRequest,
    CleaningRequestStatus,
)
from app.models.order import OTA_PLATFORM_CHANNELS, Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.owner import Owner
from app.models.room import Room
from app.models.expense import Expense, ExpenseCategory, ExpensePayer
from app.models.recon import (
    ReconBatch,
    ReconDiff,
    ReconDiffClass,
    ReconDiffStatus,
)
from app.models.settlement import OwnerSettlement, SettlementStatus
from app.models.utility_recon import (
    UtilityReconBatch,
    UtilityReconRow,
    UtilityReconSuggestion,
    UtilityReconUpload,
)
from app.core.datetime_helpers import CN_TZ, to_cn, today_cn
from app.services.audit import log_action_tx
from app.services.monthly_close.financial_lock import acquire_month_financial_lock
from app.services.monthly_close.control import ProposalFreshnessContext
from app.services.monthly_close.documents import MonthlyCloseDocumentError
from app.services.monthly_close.operating_expenses import (
    OperatingExpenseMapping,
    apply_operating_expense_row_tx,
    build_room_aliases,
    expense_id_for_business_key,
    expense_matches_operating_fact,
    operating_expense_row_fact,
    parse_operating_expense_workbook,
)
from app.services.monthly_close.operating_expense_lineage import (
    operating_predecessor_facts,
)
from app.services.monthly_close.source_contract import (
    SourceAdapterCommand,
    SourceAdapterIssue,
    SourceAdapterResult,
    bind_source_command_issues as _bind_source_command_issues,
    command_without_issue_refs as _command_without_issue_refs,
    source_evidence_is_current as _source_evidence_is_current,
    source_digest as _source_digest,
    verify_source_command as _verify_source_command,
)
from app.services.service_fee_reconciliation import (
    apply_service_fee_reconciliation,
    plan_service_fee_reconciliation,
)
from app.services.settlement_preflight import run_settlement_preflight


_OPEN_RECON_STATUSES = {ReconDiffStatus.pending, ReconDiffStatus.appeal_pending}
_CLEANING_EXPENSE_INTEGRITY_START = date(2026, 8, 1)
_MONTH_RE = re.compile(r"^\d{4}-(0[1-9]|1[0-2])$")
_OTA_ACTIONS: dict[ReconDiffClass, tuple[str, ReconDiffStatus]] = {
    ReconDiffClass.fix_amount: ("adopt", ReconDiffStatus.adopted),
    ReconDiffClass.compensation: ("adopt", ReconDiffStatus.adopted),
    ReconDiffClass.appeal: ("appeal", ReconDiffStatus.appeal_pending),
    ReconDiffClass.broken_link: ("acknowledge", ReconDiffStatus.acknowledged),
}
_OTA_PLATFORM_SCOPES = {
    "ctrip_family",
    "meituan",
    "fliggy",
    "douyin",
    "tujia",
}
_OTA_RULESET_VERSION = "monthly-close-ota-rules-v2"
_OTA_CALCULATION_VERSION = "billing-recon-engine-v1"
_OTA_APPEAL_ADJUDICATION_RULESET_VERSION = "monthly-close-ota-appeal-adjudication-v1"
_OTA_APPEAL_ADJUDICATION_CALCULATION_VERSION = "zero-money-appeal-adjudication-v1"
_OTA_ALLOWED_ACTIONS: dict[ReconDiffClass, frozenset[str]] = {
    ReconDiffClass.fix_amount: frozenset({"adopt", "dismiss"}),
    ReconDiffClass.compensation: frozenset({"adopt", "dismiss"}),
    ReconDiffClass.appeal: frozenset({"appeal", "dismiss"}),
    ReconDiffClass.broken_link: frozenset({"acknowledge", "dismiss"}),
    ReconDiffClass.manual_review: frozenset({"dismiss"}),
}
_SERVICE_RULESET_VERSION = "service-fee-reconciliation-v1"
_SERVICE_CALCULATION_VERSION = "service-fee-ledger-v1"
_OPERATING_EXPENSE_RULESET_VERSION = "operating-expense-import-v2"
_OPERATING_EXPENSE_CALCULATION_VERSION = "operating-expense-parser-v1"


async def derive_issue_lifecycle(
    db: AsyncSession, issue: MonthlyCloseIssueInstance
) -> str:
    """Project transient UI state from the exact proposal execution lineage.

    ``MonthlyCloseIssueInstance.status`` remains the durable human/domain state;
    this function never writes it.
    """

    if issue.resolution_proposal_id is None:
        return issue.status
    proposal = await db.scalar(
        select(MonthlyCloseProposal).where(
            MonthlyCloseProposal.proposal_id == issue.resolution_proposal_id,
            MonthlyCloseProposal.cycle_id == issue.cycle_id,
        )
    )
    if proposal is None or proposal.status in {"rejected", "stale", "superseded"}:
        return issue.status
    attempt = await db.scalar(
        select(MonthlyCloseExecutionAttempt)
        .where(
            MonthlyCloseExecutionAttempt.cycle_id == issue.cycle_id,
            MonthlyCloseExecutionAttempt.proposal_id == proposal.proposal_id,
        )
        .order_by(
            MonthlyCloseExecutionAttempt.attempt_no.desc(),
            MonthlyCloseExecutionAttempt.created_at.desc(),
        )
        .limit(1)
    )
    if attempt is None:
        return "proposal_pending"
    if attempt.status in {"pending", "executing"}:
        return "executing"
    if attempt.status == "unknown":
        return "unknown"
    remediation = await db.scalar(
        select(MonthlyCloseRemediation).where(
            MonthlyCloseRemediation.cycle_id == issue.cycle_id,
            MonthlyCloseRemediation.attempt_id == attempt.attempt_id,
            MonthlyCloseRemediation.status != "resolved",
        )
    )
    if attempt.status == "remediation_required" or remediation is not None:
        return "remediation"
    verification = await db.scalar(
        select(MonthlyCloseVerification)
        .where(
            MonthlyCloseVerification.cycle_id == issue.cycle_id,
            MonthlyCloseVerification.attempt_id == attempt.attempt_id,
        )
        .order_by(MonthlyCloseVerification.created_at.desc())
        .limit(1)
    )
    if attempt.status == "succeeded_unverified" or (
        verification is not None and verification.status in {"pending", "running"}
    ):
        return "verifying"
    if (
        attempt.status == "verified"
        and verification is not None
        and verification.status == "passed"
        and issue.status == "resolved"
        and issue.verification_id == verification.verification_id
    ):
        return "resolved"
    return issue.status


def source_contract_versions(proposal_type: str) -> tuple[str, str]:
    """Return the production versions authoritative for a source proposal."""
    if proposal_type == "service_fee_reconciliation":
        return _SERVICE_RULESET_VERSION, _SERVICE_CALCULATION_VERSION
    if proposal_type == "operating_expense_import":
        return (
            _OPERATING_EXPENSE_RULESET_VERSION,
            _OPERATING_EXPENSE_CALCULATION_VERSION,
        )
    if proposal_type == "utility_reconciliation":
        from app.services.monthly_close.utility_source import utility_contract_versions

        return utility_contract_versions()
    raise ValueError(f"unsupported source proposal type: {proposal_type}")


def _service_fee_fact(item: Any) -> dict[str, Any]:
    return {
        "amount": format(item.amount.quantize(Decimal("0.01")), ".2f"),
        "category": item.category.value,
        "expense_date": item.expense_date.isoformat(),
        "order_id": item.order_id,
        "owner_id": item.owner_id,
        "payer": item.payer.value,
        "room_id": item.room_id,
        "stay_group_id": item.stay_group_id,
    }


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
    from app.services.reconciliation_policy import accepted_current_order
    room_groups=defaultdict(list)
    by_id={o.order_id:o for o,_ in rows}
    for order,room in rows:
        if room is not None:room_groups[order.order_id].append(room)
    accepted={key for key,order in by_id.items() if accepted_current_order(order,room_groups[key],cycle.billing_month)}
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
            if not (order.platform_order_id or "").strip() and order.order_id not in accepted:
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
            elif (order.platform_order_id or "").strip():
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
                select(MonthlyCloseDocument)
                .options(undefer(MonthlyCloseDocument.content))
                .where(
                    MonthlyCloseDocument.cycle_id == cycle_id,
                    MonthlyCloseDocument.source_type.in_(source_types),
                    MonthlyCloseDocument.is_active.is_(True),
                )
            )
        ).scalars()
    )


def _service_context_from_adapter(
    cycle: MonthlyCloseCycle,
    adapter: SourceAdapterResult,
) -> ProposalFreshnessContext:
    commands = [_command_without_issue_refs(item) for item in adapter.commands]
    evidence_material = {
        "commands": commands,
        "evidence_refs": list(adapter.evidence_refs),
        "issues": [item.model_dump(mode="json") for item in adapter.issues],
        "source_type": adapter.source_type,
        "verification_query": adapter.verification_query,
    }
    subject_versions = {
        command["subject_id"]: next(
            ref["content_hash"]
            for ref in command["evidence_refs"]
            if ref.get("kind") == "service_plan"
        )
        for command in commands
    }
    mapping_versions = {
        ref["document_id"]: ref["mapping_hash"]
        for ref in adapter.evidence_refs
        if ref.get("kind") == "document"
    }
    return ProposalFreshnessContext(
        evidence_hash=_source_digest(evidence_material),
        subject_versions=subject_versions,
        active_input_set_hash=_source_digest(list(adapter.evidence_refs)),
        mapping_versions=mapping_versions,
        ruleset_version=_SERVICE_RULESET_VERSION,
        calculation_version=_SERVICE_CALCULATION_VERSION,
        configuration_snapshot_hash=_source_digest(
            {
                "commands": commands,
                "ruleset_version": _SERVICE_RULESET_VERSION,
            }
        ),
        control_version=cycle.control_version,
    )


async def load_service_context(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    commands: Sequence[dict[str, Any]],
) -> ProposalFreshnessContext:
    adapter = await adapt_service_source(db, cycle)
    expected = {
        item.business_idempotency_key: _command_without_issue_refs(item)
        for item in adapter.commands
    }
    supplied = {
        str(item.get("business_idempotency_key")): _command_without_issue_refs(item)
        for item in commands
    }
    if expected != supplied:
        from app.services.monthly_close.control import MonthlyCloseControlError

        raise MonthlyCloseControlError(
            "service_command_stale",
            "服务费确定性计划已经变化，请重新生成方案",
        )
    return _service_context_from_adapter(cycle, adapter)


async def build_service_proposal(
    db: AsyncSession,
    cycle_id: str,
    actor: Any,
    *,
    request_id: str | None = None,
):
    """Build a server-owned service-fee proposal from exact domain plans."""
    from app.services.monthly_close.control import MonthlyCloseControlError, create_proposal

    cycle = await db.get(MonthlyCloseCycle, cycle_id)
    if cycle is None:
        raise MonthlyCloseControlError("cycle_not_found", "月结周期不存在")
    adapter = await adapt_service_source(db, cycle)
    if not adapter.commands:
        code = (
            "service_source_needs_confirmation"
            if adapter.issues
            else "service_source_no_action_required"
        )
        raise MonthlyCloseControlError(
            code,
            "服务费来源没有可执行的确定性变更",
        )
    issue_rows, commands = await _bind_source_command_issues(
        db,
        cycle,
        adapter,
        adapter_type="service_fee_reconciliation",
    )
    context = _service_context_from_adapter(cycle, adapter)
    proposal = await create_proposal(
        db,
        cycle,
        actor,
        commands,
        evidence_hash=context.evidence_hash,
        request_id=request_id,
        proposal_type="service_fee_reconciliation",
        server_owned_commands=True,
        semantic_source_submission=True,
        subject_versions=context.subject_versions,
        active_input_set_hash=context.active_input_set_hash,
        mapping_versions=context.mapping_versions,
        ruleset_version=context.ruleset_version,
        calculation_version=context.calculation_version,
        configuration_snapshot_hash=context.configuration_snapshot_hash,
        evidence_refs=adapter.evidence_refs,
        impact_snapshot={
            "change_count": len(commands),
            "required_approver": "管理员",
            "risk": "将按现有订单规则补齐服务费账本",
            "total_amount": format(
                sum((item.amount_impact for item in adapter.commands), Decimal("0.00")),
                ".2f",
            ),
            "verification": "执行后逐笔重读费用、审计和问题归属。",
        },
    )
    for issue in issue_rows.values():
        issue.status = "proposed_fix"
        issue.resolution_proposal_id = proposal.proposal_id
    await db.commit()
    await db.refresh(proposal)
    return proposal


async def _current_service_after(
    db: AsyncSession,
    command: dict[str, Any],
) -> dict[str, Any]:
    facts: list[dict[str, Any]] = []
    for expected in command["after"]["fees"]:
        rows = list(
            await db.scalars(
                select(Expense).where(
                    Expense.is_deleted.is_(False),
                    Expense.is_service_fee.is_(True),
                    Expense.owner_id == expected["owner_id"],
                    Expense.order_id == expected["order_id"],
                    Expense.room_id == expected["room_id"],
                    Expense.category == ExpenseCategory(expected["category"]),
                    Expense.expense_date == date.fromisoformat(
                        expected["expense_date"]
                    ),
                )
            )
        )
        if len(rows) != 1:
            continue
        row = rows[0]
        facts.append(
            {
                "amount": format(Decimal(row.amount).quantize(Decimal("0.01")), ".2f"),
                "category": row.category.value,
                "expense_date": row.expense_date.isoformat(),
                "order_id": row.order_id,
                "owner_id": row.owner_id,
                "payer": row.payer.value,
                "room_id": row.room_id,
                "stay_group_id": expected.get("stay_group_id"),
            }
        )
    return {
        "fees": sorted(
            facts,
            key=lambda item: (item["category"], item["order_id"], item["room_id"]),
        )
    }


async def execute_service_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    actor: Any,
    request_id: str,
) -> dict[str, Any]:
    """Apply one exact service plan inside the caller-owned transaction."""
    from app.services.monthly_close.control import MonthlyCloseControlError

    await acquire_month_financial_lock(db, cycle.billing_month)
    locked_cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_cycle is None or locked_cycle.write_control_owner != "assistant":
        raise MonthlyCloseControlError(
            "service_control_owner_changed", "服务费写入控制权已经变化"
        )
    adapter = await adapt_service_source(db, locked_cycle)
    current = next(
        (
            item
            for item in adapter.commands
            if item.business_idempotency_key
            == command.get("business_idempotency_key")
        ),
        None,
    )
    if current is None or _command_without_issue_refs(
        current
    ) != _command_without_issue_refs(command):
        raise MonthlyCloseControlError(
            "service_command_stale", "服务费确定性计划已经变化，请重新生成方案"
        )
    owner_id = str(command["before"]["owner_id"])
    year, month = (int(part) for part in cycle.billing_month.split("-"))
    plan = await plan_service_fee_reconciliation(db, owner_id, year, month)
    await apply_service_fee_reconciliation(db, plan, actor.user_id)
    actual = await _current_service_after(db, command)
    if actual != command["after"]:
        raise MonthlyCloseControlError(
            "service_execution_effect_mismatch",
            "服务费执行结果与批准方案不一致，已安全回滚",
        )
    await log_action_tx(
        db,
        actor.user_id,
        "monthly_close.service_fee_reconciliation.execute",
        "owner",
        owner_id,
        before_data=command["before"],
        after_data={
            "approved_after": command["after"],
            "business_idempotency_key": command["business_idempotency_key"],
            "proposal_id": proposal.proposal_id,
            "request_id": request_id,
        },
    )
    await db.flush()
    audit_id = await db.scalar(
        select(func.max(AuditLog.log_id)).where(
            AuditLog.action == "monthly_close.service_fee_reconciliation.execute",
            AuditLog.resource_id == owner_id,
            AuditLog.operator_id == actor.user_id,
        )
    )
    execution_result = _service_execution_audit_identity(command)["result"]
    return {
        "result": execution_result,
        "audit_refs": [str(audit_id)] if audit_id is not None else [],
        "execution_audits": (
            [
                {
                    "audit_ref": str(audit_id),
                    **_service_execution_audit_identity(command),
                }
            ]
            if audit_id is not None
            else []
        ),
    }


def _service_execution_audit_identity(command: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "monthly_close.service_fee_reconciliation.execute",
        "resource_type": "owner",
        "resource_id": command["before"]["owner_id"],
        "result": {
            "fee_count": len(command["after"]["fees"]),
            "owner_id": command["before"]["owner_id"],
        },
    }


async def verify_service_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    attempt: Any,
) -> dict[str, Any]:
    actual = await _current_service_after(db, command)
    current_adapter = await adapt_service_source(db, cycle)
    evidence_current = _source_evidence_is_current(
        cycle=cycle,
        proposal=proposal,
        current_adapter=current_adapter,
        ruleset_version=_SERVICE_RULESET_VERSION,
        calculation_version=_SERVICE_CALCULATION_VERSION,
    )
    owner_id = command["before"]["owner_id"]
    year, month = (int(part) for part in cycle.billing_month.split("-"))
    current_plan = await plan_service_fee_reconciliation(db, owner_id, year, month)
    current_expected = {
        "fees": sorted(
            (_service_fee_fact(item) for item in current_plan.expected),
            key=lambda item: (item["category"], item["order_id"], item["room_id"]),
        )
    }
    evidence_current = evidence_current and current_expected == command["after"]
    return await _verify_source_command(
        db,
        proposal,
        command,
        attempt,
        actual=actual,
        expected_audit_identity=_service_execution_audit_identity,
        evidence_current=evidence_current,
    )


async def adapt_service_source(
    db: AsyncSession, cycle: MonthlyCloseCycle
) -> SourceAdapterResult:
    """Translate the deterministic service-fee plan into the shared contract."""
    year, month = (int(part) for part in cycle.billing_month.split("-"))
    owner_ids = list(
        (await db.execute(select(Owner.owner_id).order_by(Owner.owner_id))).scalars()
    )
    issues: list[SourceAdapterIssue] = []
    commands: list[SourceAdapterCommand] = []
    for owner_id in owner_ids:
        plan = await plan_service_fee_reconciliation(db, owner_id, year, month)
        for unresolved in plan.unresolved:
            issue_key = ":".join(
                (
                    "service",
                    cycle.billing_month,
                    owner_id,
                    unresolved.reason,
                    unresolved.order_id or unresolved.stay_group_id or "unknown",
                )
            )
            issues.append(
                SourceAdapterIssue(
                    issue_key=issue_key,
                    code="service_fee_unresolved",
                    message="系统服务费来源无法安全判断。",
                    evidence={
                        "owner_id": owner_id,
                        "order_ids": list(unresolved.order_ids),
                        "reason": unresolved.reason,
                        "room_ids": list(unresolved.room_ids),
                    },
                )
            )
        if plan.unresolved or (not plan.missing and not plan.corrections):
            continue
        planned_fees = sorted(
            [*plan.missing, *(item.expected for item in plan.corrections)],
            key=lambda item: (
                item.category.value,
                item.order_id,
                item.room_id,
                item.expense_date,
            ),
        )
        fee_facts = [_service_fee_fact(item) for item in planned_fees]
        active_expense_ids = sorted(item.expense_id for item in plan.corrections)
        subject_id = f"service:{cycle.billing_month}:{owner_id}"
        plan_evidence = {
            "billing_month": cycle.billing_month,
            "corrections": [
                {
                    "current_amount": format(
                        item.current_amount.quantize(Decimal("0.01")), ".2f"
                    ),
                    "current_expense_date": item.current_expense_date.isoformat(),
                    "current_payer": item.current_payer.value,
                    "expense_id": item.expense_id,
                    "expected": _service_fee_fact(item.expected),
                }
                for item in sorted(plan.corrections, key=lambda value: value.expense_id)
            ],
            "fees": fee_facts,
            "owner_id": owner_id,
        }
        evidence_refs = [
            {
                "control_version": cycle.control_version,
                "cycle_id": cycle.cycle_id,
                "kind": "cycle",
            },
            {"kind": "ruleset", "ruleset_version": _SERVICE_RULESET_VERSION},
            {
                "calculation_version": _SERVICE_CALCULATION_VERSION,
                "content_hash": _source_digest(plan_evidence),
                "kind": "service_plan",
                "owner_id": owner_id,
            },
        ]
        missing_amount = sum(
            (item.amount for item in plan.missing), Decimal("0.00")
        )
        correction_impact = sum(
            (
                item.expected.amount - item.current_amount
                for item in plan.corrections
            ),
            Decimal("0.00"),
        )
        commands.append(
            SourceAdapterCommand(
                command_type="service_fee_reconciliation",
                subject_id=subject_id,
                before={
                    "active_expense_ids": active_expense_ids,
                    "billing_month": cycle.billing_month,
                    "cycle_id": cycle.cycle_id,
                    "owner_id": owner_id,
                },
                after={"fees": fee_facts},
                amount_impact=missing_amount + correction_impact,
                evidence_refs=evidence_refs,
                business_idempotency_key=subject_id,
            )
        )
        for fact in fee_facts:
            fee_key = ":".join(
                (
                    subject_id,
                    fact["order_id"],
                    fact["room_id"],
                    fact["category"],
                )
            )
            issues.append(
                SourceAdapterIssue(
                    issue_key=fee_key,
                    code="service_fee_change_required",
                    message="系统服务费账本需要按确定性订单规则补齐。",
                    evidence=fact,
                    command_key=subject_id,
                )
            )
    state: Literal["missing", "needs_confirmation", "ready", "completed", "blocked"]
    has_uncommanded_issue = any(issue.command_key is None for issue in issues)
    state = (
        "needs_confirmation"
        if has_uncommanded_issue
        else "ready"
        if commands
        else "completed"
    )
    return SourceAdapterResult(
        source_type="service_fees",
        state=state,
        issues=tuple(sorted(issues, key=lambda item: item.issue_key)),
        commands=tuple(commands),
        verification_query={
            "billing_month": cycle.billing_month,
            "cycle_id": cycle.cycle_id,
            "owner_ids": owner_ids,
        },
        evidence_refs=tuple(
            sorted(
                [
                    {"kind": "ruleset", "ruleset_version": _SERVICE_RULESET_VERSION},
                    {
                        "control_version": cycle.control_version,
                        "cycle_id": cycle.cycle_id,
                        "kind": "cycle",
                    },
                ],
                key=lambda item: 0 if item["kind"] == "ruleset" else 1,
            )
        ),
    )


async def _operating_documents_and_rows(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
) -> tuple[
    list[MonthlyCloseDocument],
    list[tuple[MonthlyCloseDocument, Any]],
    list[SourceAdapterIssue],
]:
    documents = list(
        (
            await db.execute(
                select(MonthlyCloseDocument)
                .options(undefer(MonthlyCloseDocument.content))
                .where(
                    MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                    MonthlyCloseDocument.source_type == "operating_expenses",
                    MonthlyCloseDocument.is_active.is_(True),
                )
                .order_by(MonthlyCloseDocument.document_id)
            )
        ).scalars()
    )
    room_rows = list((await db.execute(select(Room.room_id, Room.room_name))).tuples())
    valid_room_ids = {room_id for room_id, _room_name in room_rows}
    room_aliases = build_room_aliases(room_rows)
    occurrences: list[tuple[MonthlyCloseDocument, Any]] = []
    issues: list[SourceAdapterIssue] = []
    parsed_documents: list[tuple[MonthlyCloseDocument, Any]] = []
    for document in documents:
        stored = (document.metadata_ or {}).get("mapping")
        mapping = None
        if isinstance(stored, dict):
            try:
                mapping = OperatingExpenseMapping(
                    sheet=stored["sheet"],
                    header_row=stored["header_row"],
                    columns=stored["columns"],
                    category_values=stored.get("category_values", {}),
                    payer_values=stored.get("payer_values", {}),
                )
            except (KeyError, TypeError):
                mapping = None
        if mapping is None:
            issues.append(
                SourceAdapterIssue(
                    issue_key=f"operating-document:{document.document_id}",
                    code="operating_expense_mapping_required",
                    message="运营支出表格结构需要人工确认。",
                    evidence={
                        "document_id": document.document_id,
                        "sha256": document.sha256,
                    },
                )
            )
            continue
        try:
            parsed = parse_operating_expense_workbook(
                document.content,
                document.filename,
                cycle.billing_month,
                valid_room_ids=valid_room_ids,
                room_aliases=room_aliases,
                mapping=mapping,
            )
        except MonthlyCloseDocumentError as exc:
            issues.append(
                SourceAdapterIssue(
                    issue_key=f"operating-document:{document.document_id}",
                    code=exc.code,
                    message="运营支出原表无法形成确定性方案。",
                    evidence={
                        "document_id": document.document_id,
                        "sha256": document.sha256,
                    },
                )
            )
            continue
        for failure in parsed.failed:
            issues.append(
                SourceAdapterIssue(
                    issue_key=(
                        f"operating-row:{document.document_id}:"
                        f"{failure.get('row', 'unknown')}"
                    ),
                    code="operating_expense_row_invalid",
                    message="运营支出原表有一行不能安全导入。",
                    evidence={
                        "document_id": document.document_id,
                        "error": str(failure.get("errors", "")),
                        "row_number": failure.get("row"),
                        "sha256": document.sha256,
                    },
                )
            )
        parsed_documents.append((document, parsed))

    predecessor_documents: list[tuple[MonthlyCloseDocument, Any]] = []
    for document, _parsed in parsed_documents:
        try:
            predecessor_documents.extend(
                await operating_predecessor_facts(db, cycle, document)
            )
        except MonthlyCloseDocumentError:
            # The exact persisted mapping below fails closed when its predecessor
            # evidence cannot be reloaded or parsed.
            continue
    parsed_by_document = {
        document.document_id: parsed for document, parsed in parsed_documents
    }
    parsed_by_document.update(
        {
            document.document_id: parsed
            for document, parsed in predecessor_documents
        }
    )
    document_by_id = {document.document_id: document for document in documents}
    document_by_id.update(
        {
            document.document_id: document
            for document, _parsed in predecessor_documents
        }
    )
    for document, parsed in parsed_documents:
        metadata = document.metadata_ or {}
        lineage = str(metadata.get("operating_event_lineage") or document.document_id)
        normalized_sheet = re.sub(r"\s+", "", parsed.mapping.sheet).casefold()
        predecessor_ids = sorted(
            item
            for item in metadata.get("supersedes_document_ids", [])
            if isinstance(item, str)
        )
        persisted_mappings = metadata.get("operating_predecessor_event_mappings", {})
        persisted_mappings = (
            persisted_mappings if isinstance(persisted_mappings, dict) else {}
        )
        for row in parsed.rows:
            if row.external_reference:
                event_key = (
                    f"{cycle.billing_month}|external-reference|{row.external_reference}"
                )
            else:
                event_key = "|".join(
                    (
                        cycle.billing_month,
                        "document-lineage",
                        lineage,
                        normalized_sheet,
                        str(row.row_number),
                    )
                )
                if predecessor_ids:
                    slot = f"{normalized_sheet}|{row.row_number}"
                    persisted = persisted_mappings.get(slot)
                    same_slot_exists = any(
                        predecessor_row.row_number == row.row_number
                        and re.sub(
                            r"\s+", "", predecessor.mapping.sheet
                        ).casefold()
                        == normalized_sheet
                        and predecessor_row.external_reference is None
                        for predecessor_id in predecessor_ids
                        if (predecessor := parsed_by_document.get(predecessor_id))
                        is not None
                        for predecessor_row in predecessor.rows
                    )
                    if persisted is not None:
                        predecessor_id = (
                            persisted.get("predecessor_document_id")
                            if isinstance(persisted, dict)
                            else None
                        )
                        predecessor_row_number = (
                            persisted.get("predecessor_row_number")
                            if isinstance(persisted, dict)
                            else None
                        )
                        predecessor_event_key = (
                            persisted.get("predecessor_event_key")
                            if isinstance(persisted, dict)
                            else None
                        )
                        predecessor = parsed_by_document.get(str(predecessor_id))
                        predecessor_document = document_by_id.get(str(predecessor_id))
                        predecessor_row = next(
                            (
                                item
                                for item in predecessor.rows
                                if item.row_number == predecessor_row_number
                                and item.external_reference is None
                            ),
                            None,
                        ) if predecessor is not None else None
                        predecessor_lineage = str(
                            (predecessor_document.metadata_ or {}).get(
                                "operating_event_lineage"
                            )
                            or predecessor_document.document_id
                        ) if predecessor_document is not None else ""
                        expected_predecessor_event_key = (
                            "|".join(
                                (
                                    cycle.billing_month,
                                    "document-lineage",
                                    predecessor_lineage,
                                    re.sub(
                                        r"\s+", "", predecessor.mapping.sheet
                                    ).casefold(),
                                    str(predecessor_row_number),
                                )
                            )
                            if predecessor is not None and predecessor_row is not None
                            else None
                        )
                        if (
                            predecessor_id not in predecessor_ids
                            or predecessor_event_key != expected_predecessor_event_key
                        ):
                            issues.append(
                                SourceAdapterIssue(
                                    issue_key=(
                                        f"operating-predecessor:{document.document_id}:"
                                        f"{normalized_sheet}:{row.row_number}"
                                    ),
                                    code="operating_expense_predecessor_mapping_invalid",
                                    message="运营支出前版行映射已变化，需要重新确认。",
                                    evidence={
                                        "document_id": document.document_id,
                                        "predecessor_document_ids": predecessor_ids,
                                        "source_row_number": row.row_number,
                                        "source_sheet": parsed.mapping.sheet,
                                    },
                                )
                            )
                            continue
                        event_key = str(predecessor_event_key)
                    elif not same_slot_exists:
                        issues.append(
                            SourceAdapterIssue(
                                issue_key=(
                                    f"operating-predecessor:{document.document_id}:"
                                    f"{normalized_sheet}:{row.row_number}"
                                ),
                                code="operating_expense_predecessor_mapping_required",
                                message="运营支出行在替代原件中移动，需要明确对应的前版行。",
                                evidence={
                                    "document_id": document.document_id,
                                    "predecessor_document_ids": predecessor_ids,
                                    "source_row_number": row.row_number,
                                    "source_sheet": parsed.mapping.sheet,
                                },
                            )
                        )
                        continue
            occurrences.append(
                (
                    document,
                    replace(row, event_key=event_key, business_key=event_key),
                )
            )
    return documents, occurrences, issues


async def adapt_operating_expense_source(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
) -> SourceAdapterResult:
    """Normalize exact operating-expense rows without document-based identity."""
    documents, occurrences, issues = await _operating_documents_and_rows(db, cycle)
    document_refs = [
        {
            "document_id": document.document_id,
            "content_sha256": hashlib.sha256(document.content).hexdigest(),
            "derived_rows_hash": _source_digest(
                [
                    operating_expense_row_fact(row)
                    for source_document, row in occurrences
                    if source_document.document_id == document.document_id
                ]
            ),
            "kind": "document",
            "mapping_hash": _source_digest(
                {
                    "mapping": (document.metadata_ or {}).get("mapping") or {},
                    "predecessor_events": (document.metadata_ or {}).get(
                        "operating_predecessor_event_mappings"
                    ) or {},
                }
            ),
            "sha256": document.sha256,
            "source_type": document.source_type,
        }
        for document in documents
    ]
    grouped: dict[str, list[tuple[MonthlyCloseDocument, Any]]] = defaultdict(list)
    for document, row in occurrences:
        grouped[row.event_key].append((document, row))
    commands: list[SourceAdapterCommand] = []
    for business_key, group in sorted(grouped.items()):
        facts = {
            json.dumps(
                operating_expense_row_fact(row),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            for _document, row in group
        }
        subject_id = f"expense:{cycle.billing_month}:{_source_digest(business_key)[:24]}"
        if len(facts) != 1:
            issues.append(
                SourceAdapterIssue(
                    issue_key=subject_id,
                    code="operating_expense_event_correction_conflict",
                    message="同一运营支出事件对应了不同内容，需要明确纠正方案。",
                    evidence={
                        "business_key_hash": _source_digest(business_key),
                        "document_ids": sorted(
                            {document.document_id for document, _row in group}
                        ),
                        "event_lineage": (
                            business_key.split("|", 3)[2]
                            if "|document-lineage|" in business_key
                            else None
                        ),
                    },
                )
            )
            continue
        fact = operating_expense_row_fact(group[0][1])
        existing = await db.get(Expense, expense_id_for_business_key(business_key))
        if existing is not None:
            if not expense_matches_operating_fact(existing, fact):
                issues.append(
                    SourceAdapterIssue(
                        issue_key=subject_id,
                        code="operating_expense_event_correction_conflict",
                        message="同一运营支出事件已存在不同费用，需要明确纠正方案。",
                        evidence={
                            "business_key_hash": _source_digest(business_key),
                            "expense_id": existing.expense_id,
                        },
                    )
                )
            continue
        row_refs = [
            {
                "business_key_hash": _source_digest(business_key),
                "document_id": document.document_id,
                "document_sha256": hashlib.sha256(document.content).hexdigest(),
                "kind": "expense_row",
                "mapping_hash": _source_digest(
                    {
                        "mapping": (document.metadata_ or {}).get("mapping") or {},
                        "predecessor_events": (document.metadata_ or {}).get(
                            "operating_predecessor_event_mappings"
                        ) or {},
                    }
                ),
                "row_number": row.row_number,
            }
            for document, row in sorted(
                group, key=lambda item: (item[0].document_id, item[1].row_number)
            )
        ]
        commands.append(
            SourceAdapterCommand(
                command_type="operating_expense_import",
                subject_id=subject_id,
                before={
                    "billing_month": cycle.billing_month,
                    "cycle_id": cycle.cycle_id,
                    "expense_id": fact["expense_id"],
                    "present": False,
                },
                after=fact,
                amount_impact=group[0][1].amount,
                evidence_refs=[
                    {
                        "control_version": cycle.control_version,
                        "cycle_id": cycle.cycle_id,
                        "kind": "cycle",
                    },
                    {
                        "calculation_version": _OPERATING_EXPENSE_CALCULATION_VERSION,
                        "kind": "ruleset",
                        "ruleset_version": _OPERATING_EXPENSE_RULESET_VERSION,
                    },
                    *row_refs,
                ],
                business_idempotency_key=subject_id,
            )
        )
        issues.append(
            SourceAdapterIssue(
                issue_key=subject_id,
                code="operating_expense_import_required",
                message="运营支出原表有一笔待管理员批准入账。",
                evidence={
                    "amount": fact["amount"],
                    "business_key_hash": _source_digest(business_key),
                    "expense_id": fact["expense_id"],
                },
                command_key=subject_id,
            )
        )
    has_uncommanded = any(issue.command_key is None for issue in issues)
    state: Literal["missing", "needs_confirmation", "ready", "completed", "blocked"]
    if not documents:
        state = "missing"
    elif has_uncommanded:
        state = "needs_confirmation"
    elif commands:
        state = "ready"
    else:
        state = "completed"
    return SourceAdapterResult(
        source_type="operating_expenses",
        state=state,
        issues=tuple(sorted(issues, key=lambda item: item.issue_key)),
        commands=tuple(commands),
        verification_query={
            "billing_month": cycle.billing_month,
            "cycle_id": cycle.cycle_id,
            "document_ids": [document.document_id for document in documents],
            "expense_ids": sorted(command.after["expense_id"] for command in commands),
        },
        evidence_refs=tuple(
            [
                {
                    "calculation_version": _OPERATING_EXPENSE_CALCULATION_VERSION,
                    "kind": "ruleset",
                    "ruleset_version": _OPERATING_EXPENSE_RULESET_VERSION,
                },
                {
                    "control_version": cycle.control_version,
                    "cycle_id": cycle.cycle_id,
                    "kind": "cycle",
                },
                *document_refs,
            ]
        ),
    )


def _operating_context_from_adapter(
    cycle: MonthlyCloseCycle,
    adapter: SourceAdapterResult,
) -> ProposalFreshnessContext:
    commands = [_command_without_issue_refs(item) for item in adapter.commands]
    material = {
        "commands": commands,
        "evidence_refs": list(adapter.evidence_refs),
        "issues": [item.model_dump(mode="json") for item in adapter.issues],
        "verification_query": adapter.verification_query,
    }
    return ProposalFreshnessContext(
        evidence_hash=_source_digest(material),
        subject_versions={
            command["subject_id"]: _source_digest(
                {"after": command["after"], "evidence_refs": command["evidence_refs"]}
            )
            for command in commands
        },
        active_input_set_hash=_source_digest(list(adapter.evidence_refs)),
        mapping_versions={
            ref["document_id"]: ref["mapping_hash"]
            for ref in adapter.evidence_refs
            if ref.get("kind") == "document"
        },
        ruleset_version=_OPERATING_EXPENSE_RULESET_VERSION,
        calculation_version=_OPERATING_EXPENSE_CALCULATION_VERSION,
        configuration_snapshot_hash=_source_digest(
            {
                "billing_month": cycle.billing_month,
                "ruleset_version": _OPERATING_EXPENSE_RULESET_VERSION,
            }
        ),
        control_version=cycle.control_version,
    )


async def load_operating_expense_context(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    _commands: Sequence[dict[str, Any]],
) -> ProposalFreshnessContext:
    return _operating_context_from_adapter(
        cycle, await adapt_operating_expense_source(db, cycle)
    )


async def build_operating_expense_proposal(
    db: AsyncSession,
    cycle_id: str,
    actor: Any,
    *,
    request_id: str | None = None,
):
    from app.services.monthly_close.control import MonthlyCloseControlError, create_proposal

    cycle = await db.get(MonthlyCloseCycle, cycle_id)
    if cycle is None:
        raise MonthlyCloseControlError("cycle_not_found", "月结周期不存在")
    adapter = await adapt_operating_expense_source(db, cycle)
    if not adapter.commands:
        raise MonthlyCloseControlError(
            "operating_expense_no_safe_commands",
            "运营支出没有可执行的确定性变更",
        )
    issue_rows, commands = await _bind_source_command_issues(
        db, cycle, adapter, adapter_type="operating_expense_import"
    )
    context = _operating_context_from_adapter(cycle, adapter)
    proposal = await create_proposal(
        db,
        cycle,
        actor,
        commands,
        evidence_hash=context.evidence_hash,
        request_id=request_id,
        proposal_type="operating_expense_import",
        evidence_refs=adapter.evidence_refs,
        subject_versions=context.subject_versions,
        active_input_set_hash=context.active_input_set_hash,
        mapping_versions=context.mapping_versions,
        ruleset_version=context.ruleset_version,
        calculation_version=context.calculation_version,
        configuration_snapshot_hash=context.configuration_snapshot_hash,
        server_owned_commands=True,
        semantic_source_submission=True,
        impact_snapshot={
            "change_count": len(commands),
            "required_approver": "管理员",
            "risk": "将写入已确认原表中的运营支出",
            "total_amount": format(
                sum((item.amount_impact for item in adapter.commands), Decimal("0.00")),
                ".2f",
            ),
            "verification": (
                "执行后逐笔重读稳定费用标识、金额、承担方、审计和问题归属。"
            ),
        },
    )
    for issue in issue_rows.values():
        issue.status = "proposed_fix"
        issue.resolution_proposal_id = proposal.proposal_id
    await db.commit()
    await db.refresh(proposal)
    return proposal


async def execute_operating_expense_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    actor: Any,
    request_id: str,
) -> dict[str, Any]:
    from app.services.monthly_close.control import MonthlyCloseControlError

    await acquire_month_financial_lock(db, cycle.billing_month)
    locked_cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_cycle is None or locked_cycle.write_control_owner != "assistant":
        raise MonthlyCloseControlError(
            "operating_expense_control_owner_changed",
            "运营支出写入控制权已经变化",
        )
    adapter = await adapt_operating_expense_source(db, locked_cycle)
    current = next(
        (
            item
            for item in adapter.commands
            if item.business_idempotency_key == command.get("business_idempotency_key")
        ),
        None,
    )
    if current is None or _command_without_issue_refs(
        current
    ) != _command_without_issue_refs(command):
        raise MonthlyCloseControlError(
            "operating_expense_command_stale",
            "运营支出原件或确认映射已经变化，请重新生成方案",
        )
    _documents, occurrences, _issues = await _operating_documents_and_rows(
        db, locked_cycle
    )
    matching_occurrences = [
        (document, row)
        for document, row in occurrences
        if row.event_key == command["after"]["business_key"]
    ]
    if not matching_occurrences:
        raise MonthlyCloseControlError(
            "operating_expense_command_stale", "批准的运营支出行已经不存在"
        )
    expense, _created = await apply_operating_expense_row_tx(
        db, matching_occurrences[0][1], actor.user_id
    )
    if not expense_matches_operating_fact(expense, command["after"]):
        raise MonthlyCloseControlError(
            "operating_expense_effect_mismatch",
            "运营支出执行结果与批准方案不一致，已安全回滚",
        )
    for source_document, source_row in matching_occurrences:
        source_sheet = next(
            (
                item.get("source_sheet")
                for item in current.evidence_refs
                if item.get("kind") == "expense_row"
                and item.get("document_id") == source_document.document_id
                and item.get("row_number") == source_row.row_number
            ),
            None,
        ) or (source_document.metadata_ or {}).get("mapping", {}).get("sheet")
        normalized_sheet = re.sub(r"\s+", "", str(source_sheet)).casefold()
        slot = f"{normalized_sheet}|{source_row.row_number}"
        metadata = dict(source_document.metadata_ or {})
        import_result = dict(metadata.get("import_result") or {})
        import_result["expense_ids"] = sorted(
            {*import_result.get("expense_ids", []), expense.expense_id}
        )
        import_result["failed"] = []
        metadata["import_result"] = import_result
        metadata["expense_business_keys"] = sorted(
            {
                *metadata.get("expense_business_keys", []),
                command["after"]["business_key"],
            }
        )
        event_links = dict(metadata.get("operating_expense_event_links") or {})
        event_links[slot] = {
            "event_key": command["after"]["business_key"],
            "expense_id": expense.expense_id,
            "source_row_number": source_row.row_number,
            "source_sheet": source_sheet,
        }
        metadata["operating_expense_event_links"] = event_links
        source_document.metadata_ = metadata
        source_document.engine_type = "expense_import"
        source_document.engine_id = "EXI-" + source_document.sha256[:12].upper()
        source_document.processing_status = "processed"
        source_document.processing_error = None
    await log_action_tx(
        db,
        actor.user_id,
        "monthly_close.operating_expense_import.execute",
        "expense",
        expense.expense_id,
        before_data=command["before"],
        after_data={
            "approved_after": command["after"],
            "business_idempotency_key": command["business_idempotency_key"],
            "proposal_id": proposal.proposal_id,
            "request_id": request_id,
        },
    )
    await db.flush()
    audit_id = await db.scalar(
        select(func.max(AuditLog.log_id)).where(
            AuditLog.action == "monthly_close.operating_expense_import.execute",
            AuditLog.resource_id == expense.expense_id,
            AuditLog.operator_id == actor.user_id,
        )
    )
    execution_result = _operating_expense_execution_audit_identity(command)["result"]
    return {
        "result": execution_result,
        "audit_refs": [str(audit_id)] if audit_id is not None else [],
        "execution_audits": (
            [
                {
                    "audit_ref": str(audit_id),
                    **_operating_expense_execution_audit_identity(command),
                }
            ]
            if audit_id is not None
            else []
        ),
    }


def _operating_expense_execution_audit_identity(
    command: dict[str, Any],
) -> dict[str, Any]:
    return {
        "action": "monthly_close.operating_expense_import.execute",
        "resource_type": "expense",
        "resource_id": command["after"]["expense_id"],
        "result": {
            "expense_id": command["after"]["expense_id"],
            "present": True,
        },
    }


async def verify_operating_expense_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    attempt: Any,
) -> dict[str, Any]:
    expense = await db.get(Expense, command["after"]["expense_id"])
    actual = (
        command["after"]
        if expense is not None
        and expense_matches_operating_fact(expense, command["after"])
        else {}
    )
    current_adapter = await adapt_operating_expense_source(db, cycle)
    evidence_current = _source_evidence_is_current(
        cycle=cycle,
        proposal=proposal,
        current_adapter=current_adapter,
        ruleset_version=_OPERATING_EXPENSE_RULESET_VERSION,
        calculation_version=_OPERATING_EXPENSE_CALCULATION_VERSION,
    )
    _documents, occurrences, current_issues = await _operating_documents_and_rows(
        db, cycle
    )
    approved_rows = [
        row
        for _document, row in occurrences
        if row.event_key == command["after"]["business_key"]
    ]
    evidence_current = (
        evidence_current
        and not current_issues
        and bool(approved_rows)
        and all(
            operating_expense_row_fact(row) == command["after"]
            for row in approved_rows
        )
    )
    return await _verify_source_command(
        db,
        proposal,
        command,
        attempt,
        actual=actual,
        expected_audit_identity=_operating_expense_execution_audit_identity,
        evidence_current=evidence_current,
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

    for issue in issues:
        if issue.get("expense_id"):
            issue["action"] = _navigate(
                "/finance",
                "在支出明细中处理",
                tab="expenses",
                month=cycle.billing_month,
                search=issue["expense_id"],
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
    legacy_contract = uses_legacy_utility_contract(cycle, documents=documents)
    batch_ids: list[str] = []
    if not legacy_contract:
        current_requirement = requirements.get("utility_expense")
        if current_requirement is None or current_requirement.state != "not_applicable":
            for document in documents:
                if document.processing_status != "processed":
                    issues.append(
                        _issue(
                            "utility_document_unprocessed",
                            document.document_id,
                            "水电支出资料尚未完成识别。",
                            document_id=document.document_id,
                            source_type="utility_expense",
                        )
                    )
    else:
        batch_ids = sorted(
            {
                document.engine_id
                for document in documents
                if document.engine_type == "utility_recon" and document.engine_id
            }
        )
        if utility_states and all(
            state == "not_applicable" for state in utility_states.values()
        ):
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
            if not documents:
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
        elif issue["code"] == "utility_document_unprocessed" and not legacy_contract:
            issue["action"] = _inline(
                "#utility-expense", "确认水电支出表格"
            )
        else:
            issue["action"] = _navigate(
                "/finance/utility-recon",
                "处理水电对账",
                month=cycle.billing_month,
                batch=issue.get("batch_id", ""),
            )
    issues.sort(key=lambda item: (item["code"], item["resource_id"]))
    summary = {
        "utility_states": utility_states,
        "utility_document_count": len(documents),
        "batch_ids": sorted(batch.batch_id for batch in batches),
        "closed_batch_count": sum(batch.status == "closed" for batch in batches),
        "operating_expense_document_count": len(operating_documents),
        "operating_expense_import_count": sum(
            document.engine_type == "expense_import" and bool(document.engine_id)
            for document in operating_documents
        ),
    }
    if not legacy_contract:
        summary["processed_utility_document_count"] = sum(
            document.processing_status == "processed" for document in documents
        )
    return _snapshot(summary, issues)


def _ota_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, Decimal):
        return format(value.quantize(Decimal("0.01")), ".2f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "value"):
        return _ota_json_value(value.value)
    if isinstance(value, dict):
        return {
            str(key): _ota_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_ota_json_value(item) for item in value]
    return str(value)


def _ota_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            _ota_json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def _ota_money(value: Decimal | None) -> str | None:
    return format(value.quantize(Decimal("0.01")), ".2f") if value is not None else None


def _ota_private_ref(value: str | None) -> str | None:
    return f"ref:{hashlib.sha256(value.encode('utf-8')).hexdigest()[:20]}" if value else None


def _ota_identity_material(
    cycle: MonthlyCloseCycle,
    batch: ReconBatch,
    diff: ReconDiff,
    action: str,
) -> dict[str, Any]:
    claim_transition = action.startswith("claim:")
    return {
        "action": action,
        "bill_amount": _ota_money(diff.bill_amount),
        "billing_month": cycle.billing_month,
        "cycle_id": cycle.cycle_id,
        "diff_class": (
            ReconDiffClass.manual_review.value if claim_transition else diff.diff_class.value
        ),
        "order_identity": None if claim_transition else _ota_private_ref(diff.order_id),
        "platform_namespace": batch.platform,
        "platform_order_identity": _ota_private_ref(diff.platform_order_id),
        "system_amount": None if claim_transition else _ota_money(diff.system_amount),
    }


def ota_issue_key(
    cycle: MonthlyCloseCycle,
    batch: ReconBatch,
    diff: ReconDiff,
    action: str,
) -> str:
    """Privacy-safe logical discrepancy identity, stable across equivalent reruns."""
    return f"ota:{_ota_digest(_ota_identity_material(cycle, batch, diff, action))}"


def _ota_issue_evidence_hash(
    cycle: MonthlyCloseCycle,
    batch: ReconBatch,
    diff: ReconDiff,
    action: str,
) -> str:
    return _ota_digest(
        {
            "identity": _ota_identity_material(cycle, batch, diff, action),
            "status": diff.status.value,
            "schema_version": "monthly-close-ota-issue-v1",
        }
    )


def _ota_platform_supported(batch: ReconBatch) -> bool:
    mapping = batch.mapping if isinstance(batch.mapping, dict) else {}
    scope = mapping.get("platform_scope")
    signature = mapping.get("layout_signature")
    if scope not in _OTA_PLATFORM_SCOPES | {"all_ota"}:
        return False
    if not isinstance(signature, str) or re.fullmatch(r"[0-9a-f]{64}", signature) is None:
        return False
    if scope == "all_ota":
        return batch.platform == f"layout:{signature[:12]}"
    return batch.platform == scope


def _ota_decision_identity(decision: dict[str, Any]) -> str:
    kind = str(decision.get("kind", ""))
    if kind == "claim":
        return f"claim:{_ota_private_ref(str(decision.get('order_id') or ''))}"
    return str(decision.get("action") or kind)


def _ota_normalize_decision(
    diff: ReconDiff,
    raw: dict[str, Any] | None,
) -> dict[str, Any]:
    from app.services.monthly_close.control import (
        MonthlyCloseControlError,
    )

    if raw is None:
        default = _OTA_ACTIONS.get(diff.diff_class)
        if default is None:
            raise MonthlyCloseControlError(
                "ota_decision_required", "该差异需要明确选择认领订单或填写忽略原因"
            )
        return {"kind": "action", "action": default[0]}
    decision = str(raw.get("decision", "")).strip()
    if decision == "claim":
        order_id = str(raw.get("order_id", "")).strip()
        if diff.diff_class != ReconDiffClass.manual_review or not order_id:
            raise MonthlyCloseControlError(
                "ota_claim_target_invalid", "只有待人工核对差异可以认领到明确订单"
            )
        return {"kind": "claim", "order_id": order_id}
    if decision == "dismiss":
        reason = str(raw.get("reason", "")).strip()
        if not reason:
            raise MonthlyCloseControlError(
                "ota_dismiss_reason_required", "忽略差异必须填写业务原因"
            )
        return {"kind": "action", "action": "dismiss", "reason": reason}
    if decision == "action":
        action = str(raw.get("action", "")).strip()
        if action not in _OTA_ALLOWED_ACTIONS.get(diff.diff_class, frozenset()):
            raise MonthlyCloseControlError(
                "ota_action_not_supported", "该差异不支持所选处置动作"
            )
        reason = str(raw.get("reason", "")).strip()
        if action == "dismiss" and not reason:
            raise MonthlyCloseControlError(
                "ota_dismiss_reason_required", "忽略差异必须填写业务原因"
            )
        return {
            "kind": "action",
            "action": action,
            **({"reason": reason} if reason else {}),
        }
    raise MonthlyCloseControlError(
        "ota_decision_invalid", "OTA差异处置决定无效"
    )


def _ota_rate(value: Decimal | None) -> str:
    return format(Decimal(value or 0).quantize(Decimal("0.0001")), ".4f")


async def _ota_compensation_state(
    db: AsyncSession,
    batch: ReconBatch,
    diff: ReconDiff,
    *,
    execution_actor_id: str | None = None,
) -> dict[str, Any]:
    if diff.diff_class != ReconDiffClass.compensation or diff.bill_amount is None:
        return {"present": False}
    amount = abs(Decimal(diff.bill_amount)).quantize(Decimal("0.01"))
    business_key = f"账单赔款 {diff.platform_order_id} {amount} ({batch.bill_month})"
    order_ids = list(
        await db.scalars(
            select(Order.order_id).where(
                Order.platform_order_id == diff.platform_order_id
            )
        )
    ) if diff.platform_order_id else []
    order_id = order_ids[0] if len(order_ids) == 1 else None
    expenses = list(
        await db.scalars(
            select(Expense)
            .where(
                Expense.description == business_key,
                Expense.is_deleted.is_(False),
            )
            .order_by(Expense.expense_id)
        )
    )
    expense = expenses[0] if expenses else None
    identity_matches = bool(
        len(expenses) == 1
        and expense is not None
        and expense.category == ExpenseCategory.other
        and Decimal(expense.amount).quantize(Decimal("0.01")) == amount
        and expense.expense_date.isoformat() == f"{batch.bill_month}-01"
        and expense.order_id == order_id
        and expense.room_id is None
        and expense.payer == ExpensePayer.company
        and not expense.is_deleted
    )
    created_by: str | None = None
    if expense is not None:
        created_by = (
            "execution_actor"
            if execution_actor_id
            and expense.created_by == execution_actor_id
            and not (diff.detail or {}).get("expense_deduped")
            else _ota_private_ref(expense.created_by)
        )
    return {
        "business_key": business_key,
        "category": (
            getattr(expense.category, "value", expense.category)
            if expense is not None
            else ExpenseCategory.other.value
        ),
        "amount": _ota_money(expense.amount if expense is not None else amount),
        "expense_date": (
            expense.expense_date.isoformat()
            if expense is not None
            else f"{batch.bill_month}-01"
        ),
        "order_ref": _ota_private_ref(expense.order_id if expense is not None else order_id),
        "room_ref": _ota_private_ref(expense.room_id) if expense is not None else None,
        "payer": (
            getattr(expense.payer, "value", expense.payer)
            if expense is not None
            else ExpensePayer.company.value
        ),
        "created_by": created_by,
        "is_deleted": bool(expense.is_deleted) if expense is not None else False,
        "present": expense is not None,
        "deduped": bool(
            expense is not None
            and (
                diff.status == ReconDiffStatus.pending
                or (diff.detail or {}).get("expense_deduped")
            )
        ),
        "identity_matches": identity_matches if expense is not None else True,
    }


async def _ota_settlement_scope(
    db: AsyncSession,
    settlement_month: str | None,
) -> tuple[list[dict[str, str]], list[str]]:
    if settlement_month is None:
        return [], []
    rows = list(
        (
            await db.execute(
                select(OwnerSettlement.settlement_id, OwnerSettlement.status)
                .where(OwnerSettlement.billing_month == settlement_month)
                .order_by(OwnerSettlement.settlement_id)
            )
        ).all()
    )
    from app.services.billing_recon.engine import _settlement_warnings

    return (
        [
            {
                "settlement_id": settlement_id,
                "status": getattr(status, "value", status),
            }
            for settlement_id, status in rows
        ],
        await _settlement_warnings(db, settlement_month),
    )


async def _ota_target_snapshot(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    batch: ReconBatch,
    diff: ReconDiff,
    decision_input: str | dict[str, Any],
    *,
    execution_actor_id: str | None = None,
) -> dict[str, Any]:
    from app.services.monthly_close.control import MonthlyCloseControlError

    decision = (
        {"kind": "action", "action": decision_input}
        if isinstance(decision_input, str)
        else dict(decision_input)
    )
    action = _ota_decision_identity(decision)
    issue_key = ota_issue_key(cycle, batch, diff, action)
    order = await db.get(Order, diff.order_id) if diff.order_id else None
    rooms = (
        list(
            await db.scalars(
                select(OrderRoom)
                .where(OrderRoom.order_id == diff.order_id)
                .order_by(OrderRoom.order_room_id)
            )
        )
        if diff.order_id
        else []
    )
    target_order = None
    if decision.get("kind") == "claim":
        target_order_id = str(decision.get("order_id", ""))
        target = await db.get(Order, target_order_id)
        if (
            target is None
            or target.is_deleted
            or (
                target.platform_order_id
                and target.platform_order_id != diff.platform_order_id
            )
        ):
            raise MonthlyCloseControlError(
                "ota_claim_target_invalid", "认领目标订单不存在、已作废或已关联其他平台单号"
            )
        other_link = await db.scalar(
            select(Order.order_id).where(
                Order.platform_order_id == diff.platform_order_id,
                Order.order_id != target.order_id,
                Order.is_deleted.is_(False),
            ).limit(1)
        ) if diff.platform_order_id else None
        duplicate_diff = await db.scalar(
            select(ReconDiff.diff_id).where(
                ReconDiff.batch_id == diff.batch_id,
                ReconDiff.order_id == target.order_id,
                ReconDiff.diff_id != diff.diff_id,
            ).limit(1)
        )
        if other_link is not None or duplicate_diff is not None:
            raise MonthlyCloseControlError(
                "ota_claim_target_invalid", "认领目标存在重复关联，不能自动选择"
            )
        target_rooms = list(
            await db.scalars(
                select(OrderRoom)
                .where(OrderRoom.order_id == target.order_id)
                .order_by(OrderRoom.order_room_id)
            )
        )
        target_order = {
            "order_id": target.order_id,
            "order_ref": _ota_private_ref(target.order_id),
            "platform_order_ref": _ota_private_ref(target.platform_order_id),
            "expected_revenue": _ota_money(target.expected_revenue),
            "actual_price": _ota_money(target.actual_price),
            "platform_commission_rate": _ota_rate(target.platform_commission_rate),
            "is_deleted": bool(target.is_deleted),
            "room_count": len(target_rooms),
        }
        target_order["version"] = _ota_digest(target_order)
        order = target
        rooms = target_rooms
    order_md = dict(order.metadata_ or {}) if order is not None else {}
    breadcrumb_key = f"bill_recon_{batch.bill_month.replace('-', '')}"
    compensation = await _ota_compensation_state(
        db, batch, diff, execution_actor_id=execution_actor_id
    )
    resolved_by = _ota_private_ref(diff.resolved_by)
    if execution_actor_id and diff.resolved_by == execution_actor_id:
        resolved_by = "execution_actor"
    anchor = None
    if order is not None:
        anchor = rooms[0].actual_price if len(rooms) == 1 and rooms[0].actual_price is not None else order.actual_price
    checkout_month = (
        order.check_out_date.strftime("%Y-%m") if order is not None else None
    )
    settlement_month = None
    if decision.get("kind") == "action" and decision.get("action") == "adopt":
        if diff.diff_class == ReconDiffClass.fix_amount:
            settlement_month = checkout_month
        elif diff.diff_class == ReconDiffClass.compensation:
            settlement_month = batch.bill_month
    settlement_scope, settlement_warnings = await _ota_settlement_scope(
        db, settlement_month
    )
    return {
        "action": action,
        "batch_id": batch.batch_id,
        "bill_amount": _ota_money(diff.bill_amount),
        "billing_month": cycle.billing_month,
        "checkout_month": checkout_month,
        "compensation_expense": compensation,
        "cycle_id": cycle.cycle_id,
        "decision": decision,
        "diff_class": diff.diff_class.value,
        "diff_id": diff.diff_id,
        "dismissal_reason": (diff.detail or {}).get("dismissal_reason"),
        "issue_key": issue_key,
        "link_matches": bool(
            order is not None
            and diff.platform_order_id
            and order.platform_order_id == diff.platform_order_id
        ),
        "actual_price": _ota_money(anchor),
        "order_ota_owner_revenue": _ota_money(order.expected_revenue) if order is not None else None,
        "ota_owner_revenue": _ota_money(Decimal(str(order_md["ota_owner_revenue"]))) if order_md.get("ota_owner_revenue") not in (None, "") else None,
        "ota_subsidy": _ota_money(Decimal(str(order_md["ota_subsidy"]))) if order_md.get("ota_subsidy") not in (None, "") else None,
        "price_locked": bool(order_md.get("price_locked")) if order is not None else None,
        "platform_commission_rate": _ota_rate(order.platform_commission_rate) if order is not None else None,
        "price_pending": bool(order.price_pending) if order is not None else None,
        "reconciliation_breadcrumb": order_md.get(breadcrumb_key),
        "order_ref": _ota_private_ref(diff.order_id),
        "platform_order_ref": _ota_private_ref(diff.platform_order_id),
        "resolved_at_present": diff.resolved_at is not None,
        "resolved_by": resolved_by,
        "room_ota_owner_revenues": [
            {
                "actual_price": _ota_money(room.actual_price),
                "room_ref": _ota_private_ref(room.order_room_id),
                "value": _ota_money(
                    Decimal(str((room.metadata_ or {}).get("ota_owner_revenue")))
                )
                if (room.metadata_ or {}).get("ota_owner_revenue") not in (None, "")
                else None,
            }
            for room in rooms
        ],
        "status": diff.status.value,
        "settlement_month": settlement_month,
        "settlement_scope": settlement_scope,
        "settlement_warnings": settlement_warnings,
        "system_amount": _ota_money(diff.system_amount),
        "target_order": target_order,
    }


def _ota_expected_after(
    before: dict[str, Any],
    diff: ReconDiff,
    decision: dict[str, Any],
) -> dict[str, Any]:
    after = json.loads(json.dumps(before, ensure_ascii=False))
    kind = decision["kind"]
    action = decision.get("action")
    if kind == "claim":
        target = after["target_order"]
        revenue = target["expected_revenue"]
        bill = after["bill_amount"]
        after["order_ref"] = target["order_ref"]
        after["link_matches"] = True
        target["platform_order_ref"] = after["platform_order_ref"]
        target["version"] = _ota_digest({key: value for key, value in target.items() if key != "version"})
        if revenue is None:
            after["status"] = ReconDiffStatus.pending.value
            after["diff_class"] = ReconDiffClass.manual_review.value
        elif bill is not None and abs(Decimal(revenue) - Decimal(bill)) <= Decimal("0.01"):
            after["status"] = ReconDiffStatus.acknowledged.value
            after["diff_class"] = ReconDiffClass.broken_link.value
            after["system_amount"] = revenue
        else:
            after["status"] = ReconDiffStatus.pending.value
            after["diff_class"] = ReconDiffClass.fix_amount.value
            after["system_amount"] = revenue
        return after
    terminal_status = {
        "adopt": ReconDiffStatus.adopted,
        "appeal": ReconDiffStatus.appeal_pending,
        "acknowledge": ReconDiffStatus.acknowledged,
        "dismiss": ReconDiffStatus.dismissed,
    }[str(action)]
    after["status"] = terminal_status.value
    if action == "dismiss":
        after["dismissal_reason"] = decision.get("reason")
    if diff.diff_class == ReconDiffClass.fix_amount and action == "adopt":
        if diff.bill_amount is None:
            raise ValueError("差异缺少账单金额，无法生成方案")
        if Decimal(diff.bill_amount) <= 0:
            raise ValueError("账单净额小于等于零，请按退款或赔款业务人工核对")
        if len(before["room_ota_owner_revenues"]) != 1:
            raise ValueError("多房单请在订单页人工处置")
        if (
            before["actual_price"] is None
            or Decimal(before["actual_price"]) <= 0
            or before.get("price_pending")
        ):
            raise ValueError("订单价格未回填，不能生成采纳方案")
        bill = _ota_money(diff.bill_amount)
        after["order_ota_owner_revenue"] = bill
        after["ota_owner_revenue"] = bill
        anchor = Decimal(before["actual_price"])
        rate = Decimal(before["platform_commission_rate"])
        after["ota_subsidy"] = (
            _ota_money(Decimal(bill) - anchor * (Decimal("1") - rate))
            if Decimal(bill) > anchor
            else None
        )
        after["price_locked"] = True
        after["reconciliation_breadcrumb"] = {
            "batch_id": before["batch_id"],
            "old_rev": before["ota_owner_revenue"],
            "old_sub": before["ota_subsidy"],
            "old_room_rev": before["room_ota_owner_revenues"][0]["value"],
            "fixed_at": today_cn().isoformat(),
        }
        after["room_ota_owner_revenues"] = [
            {**item, "value": bill if item["value"] is not None else None}
            for item in before["room_ota_owner_revenues"]
        ]
    elif diff.diff_class == ReconDiffClass.compensation and action == "adopt":
        if not after["compensation_expense"]["present"]:
            after["compensation_expense"]["present"] = True
            after["compensation_expense"]["created_by"] = "execution_actor"
    elif diff.diff_class == ReconDiffClass.broken_link and action == "acknowledge":
        after["link_matches"] = True
    after["resolved_by"] = "execution_actor"
    after["resolved_at_present"] = True
    return after


def _ota_amount_impact(diff: ReconDiff, before: dict[str, Any]) -> Decimal:
    if diff.diff_class == ReconDiffClass.fix_amount:
        if diff.bill_amount is None or diff.system_amount is None:
            raise ValueError("金额差异缺少系统值或账单值")
        return (Decimal(diff.bill_amount) - Decimal(diff.system_amount)).quantize(
            Decimal("0.01")
        )
    if diff.diff_class == ReconDiffClass.compensation:
        if diff.bill_amount is None:
            raise ValueError("赔款差异缺少账单值")
        if before["compensation_expense"]["present"]:
            return Decimal("0.00")
        return -abs(Decimal(diff.bill_amount)).quantize(Decimal("0.01"))
    return Decimal("0.00")


async def _locked_ota_selection(
    db: AsyncSession,
    cycle_id: str,
    batch_id: str,
    selected_diff_ids: Sequence[str],
    decision_diff_ids: Sequence[str] = (),
) -> tuple[MonthlyCloseCycle, ReconBatch, list[ReconDiff], list[MonthlyCloseDocument]]:
    from app.services.monthly_close.control import MonthlyCloseControlError

    selected = [str(item) for item in selected_diff_ids]
    explicit = [str(item) for item in decision_diff_ids]
    if set(selected) & set(explicit):
        raise MonthlyCloseControlError(
            "ota_selection_invalid", "同一OTA差异不能同时使用默认动作和明确决定"
        )
    selected.extend(explicit)
    if not selected:
        raise MonthlyCloseControlError(
            "ota_selection_required", "请至少选择一条可执行的OTA差异"
        )
    if len(set(selected)) != len(selected):
        raise MonthlyCloseControlError(
            "ota_selection_invalid", "OTA差异选择包含重复项"
        )
    cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if cycle is None:
        raise MonthlyCloseControlError("cycle_not_found", "月结周期不存在")
    if cycle.status == "completed":
        raise MonthlyCloseControlError("cycle_completed", "已完成月结不能创建OTA方案")
    if cycle.write_control_owner != "assistant":
        raise MonthlyCloseControlError(
            "ota_control_legacy", "当前月份仍由原对账入口控制写入"
        )
    batch = await db.scalar(
        select(ReconBatch)
        .where(ReconBatch.batch_id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if batch is None:
        raise MonthlyCloseControlError("ota_batch_not_found", "OTA对账批次不存在")
    if (
        not _MONTH_RE.fullmatch(cycle.billing_month or "")
        or not _MONTH_RE.fullmatch(batch.bill_month or "")
        or batch.bill_month != cycle.billing_month
    ):
        raise MonthlyCloseControlError(
            "ota_month_conflict", "OTA账单月份与当前月结月份不一致"
        )
    mapping = batch.mapping if isinstance(batch.mapping, dict) else {}
    if batch.status != "parsed" or mapping.get("archived_at"):
        raise MonthlyCloseControlError(
            "ota_batch_unavailable", "该OTA批次当前不能生成写入方案"
        )
    if not _ota_platform_supported(batch):
        raise MonthlyCloseControlError(
            "ota_mapping_unsupported", "该OTA批次的平台或字段映射状态不受支持"
        )
    documents = list(
        await db.scalars(
            select(MonthlyCloseDocument)
            .where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.source_type == "ota_statement",
                MonthlyCloseDocument.engine_type == "billing_recon",
                MonthlyCloseDocument.engine_id == batch.batch_id,
                MonthlyCloseDocument.is_active.is_(True),
            )
            .with_for_update()
        )
    )
    if not documents:
        raise MonthlyCloseControlError(
            "ota_batch_cycle_mismatch", "该OTA批次不属于当前月结周期"
        )
    diffs = list(
        await db.scalars(
            select(ReconDiff)
            .where(
                ReconDiff.batch_id == batch.batch_id,
                ReconDiff.diff_id.in_(selected),
            )
            .with_for_update()
        )
    )
    by_id = {item.diff_id: item for item in diffs}
    if set(by_id) != set(selected):
        raise MonthlyCloseControlError(
            "ota_selection_invalid", "所选OTA差异不完全属于指定批次"
        )
    ordered = [by_id[item] for item in selected]
    for diff in ordered:
        if diff.status != ReconDiffStatus.pending:
            raise MonthlyCloseControlError(
                "ota_diff_not_actionable", "所选OTA差异已经处理或状态已变化"
            )
        if diff.diff_class not in _OTA_ACTIONS and diff.diff_id not in set(explicit):
            raise MonthlyCloseControlError(
                "ota_action_not_supported",
                "该差异需要先人工关联或明确选择处置方式，不能隐藏代选",
            )
    return cycle, batch, ordered, documents


async def load_ota_context(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    commands: Sequence[dict[str, Any]],
):
    """Re-read the exact batch, mappings, inputs and selected diff states."""
    from app.services.monthly_close.control import ProposalFreshnessContext

    batch_ids = {
        str(command.get("before", {}).get("batch_id", "")) for command in commands
    }
    if len(batch_ids) != 1 or "" in batch_ids:
        raise ValueError("OTA commands must bind one exact batch")
    batch_id = next(iter(batch_ids))
    batch = await db.get(ReconBatch, batch_id)
    diff_ids = [str(command.get("before", {}).get("diff_id", "")) for command in commands]
    diffs = (
        list(
            await db.scalars(
                select(ReconDiff).where(
                    ReconDiff.batch_id == batch_id,
                    ReconDiff.diff_id.in_(diff_ids),
                )
            )
        )
        if batch is not None and all(diff_ids)
        else []
    )
    by_id = {item.diff_id: item for item in diffs}
    documents = list(
        await db.scalars(
            select(MonthlyCloseDocument)
            .where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.source_type == "ota_statement",
                MonthlyCloseDocument.engine_type == "billing_recon",
                MonthlyCloseDocument.is_active.is_(True),
            )
            .order_by(MonthlyCloseDocument.document_id)
        )
    )
    current_targets: list[dict[str, Any]] = []
    subject_versions: dict[str, Any] = {}
    for command in commands:
        diff_id = str(command.get("before", {}).get("diff_id", ""))
        diff = by_id.get(diff_id)
        decision = command.get("before", {}).get("decision")
        action = str(command.get("before", {}).get("action", ""))
        if batch is None or diff is None:
            target = {"action": action, "batch_id": batch_id, "diff_id": diff_id, "missing": True}
        else:
            target = await _ota_target_snapshot(
                db, cycle, batch, diff,
                decision if isinstance(decision, dict) else action,
            )
        current_targets.append(target)
        subject_versions[str(command["subject_id"])] = _ota_digest(target)
    mapping_hash = _ota_digest(batch.mapping if batch is not None else {"missing": True})
    input_material = {
        "batch_id": batch_id,
        "documents": [
            {
                "document_id": item.document_id,
                "engine_id": item.engine_id,
                "is_active": item.is_active,
                "sha256": item.sha256,
            }
            for item in documents
        ],
    }
    evidence_material = {
        "batch": (
            {
                "batch_id": batch.batch_id,
                "bill_month": batch.bill_month,
                "mapping_hash": mapping_hash,
                "platform": batch.platform,
                "row_count": batch.row_count,
                "status": batch.status,
                "summary_total": _ota_money(batch.summary_total),
            }
            if batch is not None
            else {"batch_id": batch_id, "missing": True}
        ),
        "cycle": {
            "billing_month": cycle.billing_month,
            "control_version": cycle.control_version,
            "cycle_id": cycle.cycle_id,
            "status": cycle.status,
            "write_control_owner": cycle.write_control_owner,
        },
        "inputs": input_material,
        "selected_targets": sorted(current_targets, key=lambda item: item.get("diff_id", "")),
    }
    return ProposalFreshnessContext(
        evidence_hash=_ota_digest(evidence_material),
        subject_versions=subject_versions,
        active_input_set_hash=_ota_digest(input_material),
        mapping_versions={batch_id: mapping_hash},
        ruleset_version=_OTA_RULESET_VERSION,
        calculation_version=_OTA_CALCULATION_VERSION,
        configuration_snapshot_hash=_ota_digest(
            {
                "billing_month": cycle.billing_month,
                "cycle_id": cycle.cycle_id,
                "platform": batch.platform if batch is not None else None,
            }
        ),
        control_version=cycle.control_version,
    )


async def _reconstruct_ota_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    batch: ReconBatch,
    diff: ReconDiff,
    decision: dict[str, Any],
    issue: Any,
) -> dict[str, Any]:
    from app.services.monthly_close.control import MonthlyCloseControlError

    action = _ota_decision_identity(decision)
    issue_key = ota_issue_key(cycle, batch, diff, action)
    issue_evidence_hash = _ota_issue_evidence_hash(cycle, batch, diff, action)
    before = await _ota_target_snapshot(db, cycle, batch, diff, decision)
    if (
        diff.diff_class == ReconDiffClass.compensation
        and before["compensation_expense"]["present"]
        and not before["compensation_expense"]["identity_matches"]
    ):
        raise MonthlyCloseControlError(
            "ota_compensation_conflict",
            "同一赔款业务标识已存在不同金额或归属的费用，请人工核对",
        )
    try:
        after = _ota_expected_after(before, diff, decision)
        amount_impact = _ota_amount_impact(diff, before)
    except (KeyError, ValueError) as exc:
        raise MonthlyCloseControlError("ota_action_plan_invalid", str(exc)) from exc
    return {
        "command_type": "ota_reconciliation",
        "subject_id": issue_key,
        "before": before,
        "after": after,
        "amount_impact": format(amount_impact, ".2f"),
        "evidence_refs": sorted([
            {
                "kind": "issue",
                "issue_id": issue.issue_id,
                "evidence_hash": issue_evidence_hash,
            },
            {
                "kind": "ota_batch",
                "batch_id": batch.batch_id,
                "content_hash": _ota_digest(
                    {
                        "mapping": batch.mapping,
                        "row_count": batch.row_count,
                        "summary_total": _ota_money(batch.summary_total),
                    }
                ),
            },
            {
                "kind": "ota_diff",
                "diff_id": diff.diff_id,
                "issue_key": issue_key,
                "evidence_hash": issue_evidence_hash,
            },
        ], key=lambda ref: json.dumps(ref, ensure_ascii=False, sort_keys=True, separators=(",", ":"))),
        "business_idempotency_key": issue_key,
    }


async def build_ota_proposal(
    db: AsyncSession,
    cycle_id: str,
    batch_id: str,
    selected_diff_ids: Sequence[str],
    actor: Any,
    *,
    decisions: Sequence[dict[str, Any]] = (),
    request_id: str | None = None,
):
    """Build immutable OTA commands from locked server-side reconciliation facts."""
    from app.models.monthly_close_control import MonthlyCloseIssueInstance
    from app.services.monthly_close.control import MonthlyCloseControlError, create_proposal

    decision_by_id: dict[str, dict[str, Any]] = {}
    for raw in decisions:
        if not isinstance(raw, dict):
            raise MonthlyCloseControlError("ota_decision_invalid", "OTA差异处置决定无效")
        diff_id = str(raw.get("diff_id", "")).strip()
        if not diff_id or diff_id in decision_by_id:
            raise MonthlyCloseControlError("ota_selection_invalid", "OTA差异决定包含空值或重复项")
        decision_by_id[diff_id] = raw
    cycle, batch, diffs, _documents = await _locked_ota_selection(
        db, cycle_id, batch_id, selected_diff_ids, tuple(decision_by_id)
    )
    source_subject_id = f"ota:{cycle.billing_month}:{batch.platform}"
    commands: list[dict[str, Any]] = []
    issue_rows: list[MonthlyCloseIssueInstance] = []
    change_rows: list[dict[str, Any]] = []
    for diff in diffs:
        decision = _ota_normalize_decision(diff, decision_by_id.get(diff.diff_id))
        action = _ota_decision_identity(decision)
        issue_key = ota_issue_key(cycle, batch, diff, action)
        issue_evidence_hash = _ota_issue_evidence_hash(cycle, batch, diff, action)
        issue = await db.scalar(
            select(MonthlyCloseIssueInstance)
            .where(
                MonthlyCloseIssueInstance.cycle_id == cycle.cycle_id,
                MonthlyCloseIssueInstance.adapter_type == "ota_reconciliation",
                MonthlyCloseIssueInstance.source_subject_id == source_subject_id,
                MonthlyCloseIssueInstance.issue_key == issue_key,
            )
            .with_for_update()
        )
        if issue is None:
            issue = MonthlyCloseIssueInstance(
                issue_id=f"MCI-{uuid4().hex[:20].upper()}",
                cycle_id=cycle.cycle_id,
                adapter_type="ota_reconciliation",
                source_subject_id=source_subject_id,
                issue_key=issue_key,
                status="open",
                last_seen_evidence_hash=issue_evidence_hash,
            )
            db.add(issue)
            await db.flush()
        else:
            if issue.status in {"resolved", "cancelled"}:
                issue.status = "reopened"
                issue.reopened_at = datetime.now(timezone.utc)
                issue.verification_id = None
                issue.resolution_note = None
            issue.last_seen_evidence_hash = issue_evidence_hash
        command = await _reconstruct_ota_command(
            db, cycle, batch, diff, decision, issue
        )
        before = command["before"]
        after = command["after"]
        amount_impact = Decimal(command["amount_impact"])
        commands.append(command)
        issue_rows.append(issue)
        change_rows.append(
            {
                "amount_impact": format(amount_impact, ".2f"),
                "before": before,
                "after": after,
                "evidence": {
                    "batch_id": batch.batch_id,
                    "issue_key": issue_key,
                },
            }
        )
    context = await load_ota_context(db, cycle, commands)
    proposal = await create_proposal(
        db,
        cycle,
        actor,
        commands,
        evidence_hash=context.evidence_hash,
        request_id=request_id,
        proposal_type="ota_reconciliation",
        server_owned_commands=True,
        impact_snapshot={
            "change_count": len(commands),
            "changes": change_rows,
            "required_approver": "管理员",
            "risk": "将修改订单到账、关联或OTA差异状态",
            "total_amount": format(
                sum(
                    (Decimal(command["amount_impact"]) for command in commands),
                    Decimal("0.00"),
                ),
                ".2f",
            ),
            "verification": "执行后重新读取指定批次、订单或费用、差异状态、审计和幂等记录；差异仍存在时不会标记完成。",
        },
    )
    for issue in issue_rows:
        issue.status = "proposed_fix"
        issue.resolution_proposal_id = proposal.proposal_id
    await db.commit()
    await db.refresh(proposal)
    return proposal


async def _ota_execution_rows(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    command: dict[str, Any],
) -> tuple[ReconBatch, ReconDiff, dict[str, Any]]:
    from app.services.monthly_close.control import MonthlyCloseControlError

    before = command.get("before") if isinstance(command.get("before"), dict) else {}
    batch_id = str(before.get("batch_id", ""))
    diff_id = str(before.get("diff_id", ""))
    batch = await db.scalar(
        select(ReconBatch)
        .where(ReconBatch.batch_id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    diff = await db.scalar(
        select(ReconDiff)
        .where(ReconDiff.diff_id == diff_id, ReconDiff.batch_id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if batch is None or diff is None:
        raise MonthlyCloseControlError(
            "ota_command_stale", "批准的OTA批次或差异已经不存在"
        )
    action = str(before.get("action", ""))
    if (
        before.get("cycle_id") != cycle.cycle_id
        or before.get("billing_month") != cycle.billing_month
        or batch.bill_month != cycle.billing_month
        or command.get("subject_id") != ota_issue_key(cycle, batch, diff, action)
        or command.get("business_idempotency_key") != command.get("subject_id")
    ):
        raise MonthlyCloseControlError(
            "ota_command_scope_invalid", "批准的OTA命令归属或稳定身份不匹配"
        )
    return batch, diff, before


async def _validate_reconstructed_ota_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    batch: ReconBatch,
    diff: ReconDiff,
    command: dict[str, Any],
) -> dict[str, Any]:
    from app.models.monthly_close_control import MonthlyCloseIssueInstance
    from app.services.monthly_close.control import MonthlyCloseControlError

    decision = command.get("before", {}).get("decision")
    if not isinstance(decision, dict):
        raise MonthlyCloseControlError(
            "ota_command_scope_invalid", "批准的OTA命令缺少服务端决定"
        )
    issue_ref = next(
        (
            ref for ref in command.get("evidence_refs", [])
            if isinstance(ref, dict) and ref.get("kind") == "issue"
        ),
        None,
    )
    issue = await db.scalar(
        select(MonthlyCloseIssueInstance).where(
            MonthlyCloseIssueInstance.cycle_id == cycle.cycle_id,
            MonthlyCloseIssueInstance.issue_id == (
                issue_ref.get("issue_id") if issue_ref else None
            ),
            MonthlyCloseIssueInstance.resolution_proposal_id == proposal.proposal_id,
        )
    )
    if issue is None:
        raise MonthlyCloseControlError(
            "ota_command_scope_invalid", "批准的OTA命令缺少有效问题归属"
        )
    rebuilt = await _reconstruct_ota_command(
        db, cycle, batch, diff, decision, issue
    )
    if rebuilt != command:
        raise MonthlyCloseControlError(
            "ota_command_not_server_owned", "批准的OTA命令与当前确定性计划不一致"
        )
    return decision


async def execute_ota_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    actor: Any,
    request_id: str,
) -> dict[str, Any]:
    """Execute exactly one approved OTA action without committing."""
    from app.services.billing_recon.engine import apply_diff_action_tx, claim_match_tx
    from app.services.monthly_close.control import MonthlyCloseControlError

    # Global financial lock order: month -> cycle -> batch/diff -> order.
    await acquire_month_financial_lock(db, cycle.billing_month)
    locked_cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_cycle is None or locked_cycle.write_control_owner != "assistant":
        raise MonthlyCloseControlError(
            "ota_control_owner_changed", "OTA写入控制权已经变化，请重新生成方案"
        )
    _batch, diff, before = await _ota_execution_rows(db, locked_cycle, command)
    decision = await _validate_reconstructed_ota_command(
        db, locked_cycle, proposal, _batch, diff, command
    )
    current = await _ota_target_snapshot(db, locked_cycle, _batch, diff, decision)
    if current != command["before"]:
        raise MonthlyCloseControlError(
            "ota_command_stale", "OTA差异或目标值已经变化，请重新生成方案"
        )
    if decision["kind"] == "claim":
        result = await claim_match_tx(
            db, diff, str(decision["order_id"]), actor.user_id
        )
        result = {**result, "settlement_warnings": []}
    else:
        action = str(decision["action"])
        if action == "dismiss" and decision.get("reason"):
            diff.detail = {
                **(diff.detail or {}),
                "dismissal_reason": str(decision["reason"]),
            }
        result = await apply_diff_action_tx(db, diff, action, actor.user_id)
    actual_after = await _ota_target_snapshot(
        db,
        locked_cycle,
        _batch,
        diff,
        decision,
        execution_actor_id=actor.user_id,
    )
    if actual_after != command["after"]:
        raise MonthlyCloseControlError(
            "ota_execution_effect_mismatch",
            "OTA执行结果与批准的完整财务计划不一致，已安全回滚",
        )
    await log_action_tx(
        db,
        actor.user_id,
        "monthly_close.ota_reconciliation.execute",
        "recon_diff",
        diff.diff_id,
        before_data=command["before"],
        after_data={
            "approved_after": command["after"],
            "business_idempotency_key": command["business_idempotency_key"],
            "proposal_id": proposal.proposal_id,
            "request_id": request_id,
        },
    )
    await db.flush()
    audit_id = await db.scalar(
        select(func.max(AuditLog.log_id)).where(
            AuditLog.action == "monthly_close.ota_reconciliation.execute",
            AuditLog.resource_id == diff.diff_id,
        )
    )
    return {
        "result": {
            "batch_id": diff.batch_id,
            "diff_id": diff.diff_id,
            "settlement_warnings": result["settlement_warnings"],
            "status": result["status"],
        },
        "audit_refs": [str(audit_id)] if audit_id is not None else [],
    }


async def verify_ota_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    attempt: Any,
) -> dict[str, Any]:
    """Deterministically re-read the exact business effect and durable lineage."""
    from app.models.monthly_close_control import MonthlyCloseIssueInstance

    batch, diff, before = await _ota_execution_rows(db, cycle, command)
    decision = before.get("decision")
    actual = await _ota_target_snapshot(
        db,
        cycle,
        batch,
        diff,
        decision if isinstance(decision, dict) else str(before["action"]),
        execution_actor_id=attempt.actor_id,
    )
    audits = list(
        await db.scalars(
            select(AuditLog).where(
                AuditLog.action == "monthly_close.ota_reconciliation.execute",
                AuditLog.resource_id == diff.diff_id,
                AuditLog.operator_id == attempt.actor_id,
            )
        )
    )
    audit_present = any(
        item.before_data == command["before"]
        and isinstance(item.after_data, dict)
        and item.after_data.get("approved_after") == command["after"]
        and item.after_data.get("business_idempotency_key")
        == command["business_idempotency_key"]
        and item.after_data.get("proposal_id") == proposal.proposal_id
        and item.after_data.get("request_id") == attempt.request_id
        for item in audits
    )
    issue_ref = next(
        (
            item
            for item in command.get("evidence_refs", [])
            if isinstance(item, dict) and item.get("kind") == "issue"
        ),
        None,
    )
    issue = (
        await db.scalar(
            select(MonthlyCloseIssueInstance).where(
                MonthlyCloseIssueInstance.cycle_id == cycle.cycle_id,
                MonthlyCloseIssueInstance.issue_id == issue_ref.get("issue_id"),
                MonthlyCloseIssueInstance.resolution_proposal_id == proposal.proposal_id,
            )
        )
        if issue_ref is not None
        else None
    )
    issue_hash = issue_ref.get("evidence_hash") if issue_ref is not None else None
    issue_matches = (
        issue is not None
        and issue.issue_key == command["subject_id"]
        and issue.last_seen_evidence_hash == issue_hash
        and issue.status not in {"resolved", "cancelled"}
    )
    if actual == command["after"] and audit_present and issue_matches:
        outcome = "applied"
        actual_amount = command["amount_impact"]
    elif actual == command["before"] and not audit_present:
        outcome = "not_applied"
        actual_amount = "0.00"
    else:
        outcome = "inconclusive"
        actual_amount = "0.00"
    verification_hash = _ota_digest(
        {
            "actual": actual,
            "attempt_id": attempt.attempt_id,
            "audit_present": audit_present,
            "issue_matches": issue_matches,
            "outcome": outcome,
        }
    )
    expected_version = proposal.subject_versions.get(command["subject_id"])
    return {
        "outcome": outcome,
        "business_idempotency_key": command["business_idempotency_key"],
        "subject": {
            "subject_id": command["subject_id"],
            "expected_version": expected_version,
            "actual_version": expected_version,
        },
        "target": {
            "expected_before": command["before"],
            "expected_after": command["after"],
            "actual": actual,
        },
        "amount": {
            "expected": command["amount_impact"],
            "actual": actual_amount,
        },
        "audit": {
            "present": audit_present,
            "request_id": attempt.request_id,
            "business_idempotency_key": command["business_idempotency_key"],
            "expected_before": command["before"],
            "expected_after": command["after"],
        },
        "idempotency": {
            "present": audit_present,
            "request_id": attempt.request_id,
            "business_idempotency_key": command["business_idempotency_key"],
        },
        "evidence": {
            "approved_evidence_hash": proposal.evidence_hash,
            "verification_evidence_hash": verification_hash,
        },
        "issues": (
            [
                {
                    "issue_id": issue.issue_id,
                    "approved_evidence_hash": issue_hash,
                    "actual_evidence_hash": issue.last_seen_evidence_hash,
                    "resolution": (
                        "keep_open"
                        if actual.get("status") in {
                            ReconDiffStatus.pending.value,
                            ReconDiffStatus.appeal_pending.value,
                        }
                        else "resolve"
                    ),
                }
            ]
            if issue_matches
            else []
        ),
    }


def _ota_document_input_hash(
    batch_id: str, documents: Sequence[MonthlyCloseDocument]
) -> str:
    return _ota_digest(
        {
            "batch_id": batch_id,
            "documents": [
                {
                    "document_id": item.document_id,
                    "engine_id": item.engine_id,
                    "is_active": item.is_active,
                    "sha256": item.sha256,
                }
                for item in sorted(documents, key=lambda value: value.document_id)
            ],
        }
    )


def _ota_settlement_row_fact(row: Any, platform_order_id: str | None) -> dict[str, Any]:
    material = {
        "amount": _ota_money(row.amount),
        "checkin": row.checkin.isoformat() if row.checkin else None,
        "checkout": row.checkout.isoformat() if row.checkout else None,
        "platform_order_ref": _ota_private_ref(platform_order_id),
        "row_index": row.source_row_index,
        "row_type": row.row_type,
    }
    return {**material, "row_hash": _ota_digest(material)}


def _ota_identity_choice(
    issue_id: str,
    source_row: dict[str, Any],
    order: Order,
    *,
    current_order_id: str | None,
) -> dict[str, Any]:
    channel = getattr(order.channel, "value", str(order.channel))
    material = {
        "issue_id": issue_id,
        "source_row_hash": source_row["row_hash"],
        "order_id_hash": _ota_private_ref(order.order_id),
        "channel": channel,
        "order_version": _ota_digest(
            {
                "order_id_hash": _ota_private_ref(order.order_id),
                "platform_order_ref": _ota_private_ref(order.platform_order_id),
                "channel": channel,
                "updated_at": order.updated_at.isoformat() if order.updated_at else None,
            }
        ),
    }
    return {
        "choice_id": f"OAIC-{_ota_digest(material)[:19]}",
        "order_ref": material["order_id_hash"],
        "channel": channel,
        "order_version": material["order_version"],
        "is_current_appeal": order.order_id == current_order_id,
    }


async def _verified_ota_identity_proof(
    db: AsyncSession,
    *,
    issue: Any,
    diff: ReconDiff,
    source_row: dict[str, Any] | None,
    later_cycle: MonthlyCloseCycle,
    later_document: MonthlyCloseDocument,
    later_batch: ReconBatch,
    ignored_adjudication_proposal_id: str | None = None,
) -> dict[str, Any] | None:
    """Accept identity evidence only after the exact Task 6 attempt is verified."""
    from app.models.monthly_close_control import (
        MonthlyCloseExecutionAttempt,
        MonthlyCloseProposal,
        MonthlyCloseVerification,
    )
    from app.services.monthly_close.control import (
        verified_execution_lineage_is_valid,
    )

    proof = (diff.detail or {}).get("appeal_identity_adjudication")
    if not isinstance(proof, dict) or source_row is None:
        return None
    proposal_id = (diff.detail or {}).get("appeal_identity_adjudication_proposal_id")
    if proposal_id == ignored_adjudication_proposal_id:
        return None
    proposal = await db.scalar(
        select(MonthlyCloseProposal).where(
            MonthlyCloseProposal.cycle_id == issue.cycle_id,
            MonthlyCloseProposal.proposal_id == proposal_id,
            MonthlyCloseProposal.proposal_type == "ota_appeal_adjudication",
        )
    )
    attempt = await db.scalar(
        select(MonthlyCloseExecutionAttempt)
        .where(
            MonthlyCloseExecutionAttempt.cycle_id == issue.cycle_id,
            MonthlyCloseExecutionAttempt.proposal_id == proposal_id,
            MonthlyCloseExecutionAttempt.status == "verified",
        )
        .order_by(MonthlyCloseExecutionAttempt.created_at.desc())
        .limit(1)
    )
    verification = (
        await db.scalar(
            select(MonthlyCloseVerification).where(
                MonthlyCloseVerification.cycle_id == issue.cycle_id,
                MonthlyCloseVerification.attempt_id == attempt.attempt_id,
                MonthlyCloseVerification.status == "passed",
            )
        )
        if attempt is not None
        else None
    )
    commands = (
        proposal.canonical_payload.get("commands", [])
        if proposal is not None and isinstance(proposal.canonical_payload, dict)
        else []
    )
    command = commands[0] if len(commands) == 1 and isinstance(commands[0], dict) else None
    current_order = await db.get(Order, diff.order_id) if diff.order_id else None
    expected = {
        "schema_version": "monthly-close-ota-appeal-identity-v1",
        "issue_id": issue.issue_id,
        "issue_key": issue.issue_key,
        "issue_evidence_hash": issue.last_seen_evidence_hash,
        "order_id_hash": _ota_private_ref(diff.order_id),
        "order_channel": (
            getattr(current_order.channel, "value", str(current_order.channel))
            if current_order is not None
            else None
        ),
        "order_version": (
            _ota_identity_choice(
                issue.issue_id,
                source_row,
                current_order,
                current_order_id=diff.order_id,
            )["order_version"]
            if current_order is not None
            else None
        ),
        "source_row_hash": source_row["row_hash"],
        "later_cycle_id": later_cycle.cycle_id,
        "later_document_id": later_document.document_id,
        "later_document_sha256": later_document.sha256,
        "later_batch_id": later_batch.batch_id,
        "later_batch_fingerprint": (later_batch.mapping or {}).get(
            "upload_fingerprint"
        ),
    }
    lineage_valid = bool(
        proposal is not None
        and attempt is not None
        and verification is not None
        and await verified_execution_lineage_is_valid(
            db,
            proposal_id=proposal.proposal_id,
            attempt_id=attempt.attempt_id,
            verification_id=verification.verification_id,
            cycle_id=issue.cycle_id,
            proposal_type="ota_appeal_adjudication",
            expected_audit_identity=_ota_appeal_adjudication_audit_identity,
        )
    )
    if (
        proposal is None
        or attempt is None
        or verification is None
        or command is None
        or command.get("command_type") != "ota_appeal_adjudication"
        or command.get("subject_id") != f"ota-appeal:{issue.issue_id}"
        or not lineage_valid
        or hashlib.sha256(later_document.content).hexdigest()
        != later_document.sha256
        or (command.get("after", {}).get("target") or {}).get("identity_proof")
        != proof
        or proof != expected
    ):
        return None
    return {
        "match_via": "adjudicated",
        "order_id_hash": proof["order_id_hash"],
        "order_channel": proof["order_channel"],
        "adjudication_proposal_id": proposal.proposal_id,
        "adjudication_verification_id": verification.verification_id,
    }


async def stage_ota_appeal_settlement_candidates(
    db: AsyncSession,
    later_cycle: MonthlyCloseCycle,
    later_document: MonthlyCloseDocument,
    later_batch: ReconBatch,
    *,
    replay_diff_id: str | None = None,
    ignored_adjudication_proposal_id: str | None = None,
    persist: bool = True,
) -> list[dict[str, Any]]:
    """Persist privacy-safe, evidence-only candidates from one confirmed workbook."""
    from app.models.monthly_close_control import (
        MonthlyCloseExecutionAttempt,
        MonthlyCloseIssueInstance,
        MonthlyCloseOtaSettlementConsumption,
        MonthlyCloseProposal,
        MonthlyCloseVerification,
    )
    from app.services.billing_recon.parser import (
        BillMapping,
        aggregate_orders,
        extract_bill_rows,
        load_workbook_rows,
    )
    from app.services.billing_recon.analysis import PlatformScope
    from app.services.billing_recon.engine import (
        channels_for_scope,
        fetch_candidates,
        match_orders,
    )
    from app.services.monthly_close.control import (
        verified_execution_lineage_is_valid,
    )

    sheets, datemode = await asyncio.to_thread(
        load_workbook_rows, later_document.content, later_document.filename
    )
    mapping = BillMapping.model_validate(later_batch.mapping or {})
    rows = extract_bill_rows(sheets[mapping.sheet], mapping, datemode)
    rows_by_order: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        rows_by_order[row.order_no].append(row)
    if not rows_by_order:
        if persist:
            later_batch.mapping = {
                **(later_batch.mapping or {}),
                "appeal_settlement_candidates": [],
            }
        return []

    bill_orders = aggregate_orders(rows)
    scope_value = (later_batch.mapping or {}).get("platform_scope")
    system_candidates: list[Order] = []
    try:
        later_scope = PlatformScope(str(scope_value))
        checkouts = [item.checkout for item in bill_orders.values() if item.checkout]
        system_candidates = await fetch_candidates(
            db,
            later_batch.bill_month,
            date_lo=min(checkouts) if checkouts else None,
            date_hi=max(checkouts) if checkouts else None,
            channels=channels_for_scope(later_scope),
        )
        engine_matches = match_orders(bill_orders, system_candidates)
    except (TypeError, ValueError):
        later_scope = None
        engine_matches = {}

    historical_diffs = list(
        await db.scalars(
            select(ReconDiff)
            .join(ReconBatch, ReconDiff.batch_id == ReconBatch.batch_id)
            .where(
                ReconBatch.platform == later_batch.platform,
                ReconBatch.batch_id != later_batch.batch_id,
                ReconDiff.diff_class == ReconDiffClass.appeal,
                or_(
                    ReconDiff.status == ReconDiffStatus.appeal_pending,
                    ReconDiff.diff_id == replay_diff_id,
                ),
                ReconDiff.platform_order_id.in_(sorted(rows_by_order)),
            )
            .order_by(ReconDiff.diff_id)
        )
    )
    later_documents = list(
        await db.scalars(
            select(MonthlyCloseDocument)
            .where(
                MonthlyCloseDocument.cycle_id == later_cycle.cycle_id,
                MonthlyCloseDocument.source_type == "ota_statement",
                MonthlyCloseDocument.engine_type == "billing_recon",
                MonthlyCloseDocument.is_active.is_(True),
            )
            .order_by(MonthlyCloseDocument.document_id)
        )
    )
    # The current document is linked in-memory before this helper is called and
    # may not be visible to a SELECT until the route flushes it.
    if all(item.document_id != later_document.document_id for item in later_documents):
        later_documents.append(later_document)
    later_input_hash = _ota_document_input_hash(later_batch.batch_id, later_documents)
    later_fingerprint = (later_batch.mapping or {}).get("upload_fingerprint")
    fingerprint_proven = bool(
        isinstance(later_fingerprint, str)
        and re.fullmatch(r"[0-9a-f]{64}", later_fingerprint)
        and later_fingerprint == later_document.sha256
    )
    historical_by_order = Counter(
        diff.platform_order_id for diff in historical_diffs if diff.platform_order_id
    )
    candidates: list[dict[str, Any]] = []
    for diff in historical_diffs:
        original_batch = await db.get(ReconBatch, diff.batch_id)
        original_cycle = await db.scalar(
            select(MonthlyCloseCycle).where(
                MonthlyCloseCycle.billing_month == original_batch.bill_month
            )
        ) if original_batch is not None else None
        if (
            original_batch is None
            or original_cycle is None
            or original_cycle.billing_month >= later_cycle.billing_month
        ):
            continue
        expected_issue_key = ota_issue_key(
            original_cycle, original_batch, diff, "appeal"
        )
        issues = list(
            await db.scalars(
                select(MonthlyCloseIssueInstance).where(
                    MonthlyCloseIssueInstance.cycle_id == original_cycle.cycle_id,
                    MonthlyCloseIssueInstance.issue_key == expected_issue_key,
                    MonthlyCloseIssueInstance.adapter_type == "ota_reconciliation",
                    MonthlyCloseIssueInstance.status.in_(
                        ("waiting_information", "proposed_fix")
                    ),
                )
            )
        )
        if len(issues) != 1:
            continue
        issue = issues[0]
        proposal = await db.scalar(
            select(MonthlyCloseProposal).where(
                MonthlyCloseProposal.cycle_id == original_cycle.cycle_id,
                MonthlyCloseProposal.proposal_id == issue.resolution_proposal_id,
                MonthlyCloseProposal.proposal_type == "ota_reconciliation",
            )
        )
        verification = await db.scalar(
            select(MonthlyCloseVerification).where(
                MonthlyCloseVerification.cycle_id == original_cycle.cycle_id,
                MonthlyCloseVerification.verification_id == issue.verification_id,
                MonthlyCloseVerification.status == "passed",
            )
        )
        attempt = await db.scalar(
            select(MonthlyCloseExecutionAttempt).where(
                MonthlyCloseExecutionAttempt.cycle_id == original_cycle.cycle_id,
                MonthlyCloseExecutionAttempt.attempt_id
                == (verification.attempt_id if verification is not None else None),
                MonthlyCloseExecutionAttempt.proposal_id == issue.resolution_proposal_id,
                MonthlyCloseExecutionAttempt.status == "verified",
            )
        ) if verification is not None else None
        commands = (
            proposal.canonical_payload.get("commands", [])
            if proposal is not None and isinstance(proposal.canonical_payload, dict)
            else []
        )
        matching_commands = [
            command for command in commands
            if isinstance(command, dict) and command.get("subject_id") == issue.issue_key
        ]
        issue_ref = next(
            (
                ref for ref in (matching_commands[0].get("evidence_refs", []) if len(matching_commands) == 1 else [])
                if isinstance(ref, dict)
                and ref.get("kind") == "issue"
                and ref.get("issue_id") == issue.issue_id
            ),
            None,
        )
        lineage_valid = bool(
            proposal is not None
            and attempt is not None
            and verification is not None
            and await verified_execution_lineage_is_valid(
                db,
                proposal_id=proposal.proposal_id,
                attempt_id=attempt.attempt_id,
                verification_id=verification.verification_id,
                cycle_id=original_cycle.cycle_id,
                proposal_type="ota_reconciliation",
            )
        )
        if (
            proposal is None
            or verification is None
            or attempt is None
            or len(matching_commands) != 1
            or issue_ref is None
            or issue_ref.get("evidence_hash") != issue.last_seen_evidence_hash
            or not lineage_valid
        ):
            continue
        original_documents = list(
            await db.scalars(
                select(MonthlyCloseDocument)
                .where(
                    MonthlyCloseDocument.cycle_id == original_cycle.cycle_id,
                    MonthlyCloseDocument.source_type == "ota_statement",
                    MonthlyCloseDocument.engine_type == "billing_recon",
                    MonthlyCloseDocument.is_active.is_(True),
                )
                .order_by(MonthlyCloseDocument.document_id)
            )
        )
        original_input_hash = _ota_document_input_hash(
            original_batch.batch_id, original_documents
        )
        if original_input_hash != proposal.active_input_set_hash:
            continue
        matching_rows = rows_by_order.get(diff.platform_order_id or "", [])
        exact = len(matching_rows) == 1
        engine_match = engine_matches.get(diff.platform_order_id or "")
        try:
            original_scope = PlatformScope(
                str((original_batch.mapping or {}).get("platform_scope"))
            )
            original_channel_ok = bool(
                engine_match is not None
                and engine_match.order is not None
                and engine_match.order.channel in channels_for_scope(original_scope)
            )
        except (TypeError, ValueError):
            original_channel_ok = False
        source_row = (
            _ota_settlement_row_fact(matching_rows[0], diff.platform_order_id)
            if exact
            else None
        )
        current_order = await db.get(Order, diff.order_id) if diff.order_id else None
        automatic_order_proven = bool(
            exact
            and later_scope is not None
            and engine_match is not None
            and engine_match.via == "exact"
            and engine_match.order is not None
            and engine_match.order.order_id == diff.order_id
            and engine_match.order.channel in channels_for_scope(later_scope)
            and original_channel_ok
            and fingerprint_proven
        )
        choice_orders = [
            order
            for order in system_candidates
            if order.platform_order_id == diff.platform_order_id
        ]
        if (
            current_order is not None
            and current_order.platform_order_id == diff.platform_order_id
            and all(item.order_id != current_order.order_id for item in choice_orders)
        ):
            choice_orders.append(current_order)
        identity_choices = (
            [
                _ota_identity_choice(
                    issue.issue_id,
                    source_row,
                    order,
                    current_order_id=diff.order_id,
                )
                for order in sorted(
                    choice_orders,
                    key=lambda value: (
                        getattr(value.channel, "value", str(value.channel)),
                        value.order_id,
                    ),
                )
            ]
            if source_row is not None
            else []
        )
        adjudicated_proof = await _verified_ota_identity_proof(
            db,
            issue=issue,
            diff=diff,
            source_row=source_row,
            later_cycle=later_cycle,
            later_document=later_document,
            later_batch=later_batch,
            ignored_adjudication_proposal_id=(
                ignored_adjudication_proposal_id
                if diff.diff_id == replay_diff_id
                else None
            ),
        )
        order_proven = automatic_order_proven or adjudicated_proof is not None
        competing_appeals = historical_by_order[diff.platform_order_id] != 1
        settled_amount = matching_rows[0].amount if exact else None
        short_paid = bool(
            exact
            and diff.system_amount is not None
            and (diff.system_amount - settled_amount) > Decimal("0.01")
        )
        row_identity_hash = (
            _ota_digest(
                {
                    "schema_version": "monthly-close-ota-row-identity-v1",
                    "later_cycle_id": later_cycle.cycle_id,
                    "later_document_id": later_document.document_id,
                    "later_batch_id": later_batch.batch_id,
                    "platform_namespace": later_batch.platform,
                    "source_row_index": source_row["row_index"],
                }
            )
            if source_row is not None
            else None
        )
        consumption = (
            await db.scalar(
                select(MonthlyCloseOtaSettlementConsumption).where(
                    MonthlyCloseOtaSettlementConsumption.row_identity_hash
                    == row_identity_hash
                )
            )
            if row_identity_hash is not None
            else None
        )
        consumed_elsewhere = bool(
            consumption is not None
            and consumption.original_issue_id != issue.issue_id
        )
        if not exact:
            reason_code = "duplicate_source_rows"
            recovery_kind = "archive_reupload"
            action = "该订单在后续账单出现多行，需先核对并上传唯一到账行"
        elif consumed_elsewhere:
            reason_code = "row_already_consumed"
            recovery_kind = "refresh_history"
            action = "该到账行已绑定其他申诉，请查看核销历史"
        elif competing_appeals:
            reason_code = "multiple_historical_appeals"
            recovery_kind = "adjudicate_appeals"
            action = "同一笔到账匹配多条历史申诉，请先核对并保留唯一申诉"
        elif not order_proven:
            current_channel = (
                getattr(current_order.channel, "value", str(current_order.channel))
                if current_order is not None
                else None
            )
            later_channel_ok = bool(
                current_order is not None
                and later_scope is not None
                and current_order.channel in channels_for_scope(later_scope)
            )
            try:
                original_channel_current_ok = bool(
                    current_order is not None
                    and current_order.channel in channels_for_scope(original_scope)
                )
            except (NameError, TypeError, ValueError):
                original_channel_current_ok = False
            reason_code = (
                "channel_platform_conflict"
                if current_channel is not None
                and not (later_channel_ok and original_channel_current_ok)
                else "system_order_conflict"
            )
            recovery_kind = "confirm_identity"
            action = "到账行无法唯一证明对应的平台订单，请先核对订单渠道和平台单号"
        elif short_paid:
            reason_code = "short_payment"
            recovery_kind = "retain_appeal"
            action = "本次到账少于原申诉金额，请继续向平台追款"
        else:
            reason_code = "ready_to_reconcile"
            recovery_kind = "reconcile"
            action = "确认该笔申诉已到账"
        order_proof = adjudicated_proof or (
            {
                "match_via": engine_match.via,
                "order_id_hash": _ota_private_ref(engine_match.order.order_id),
                "order_channel": engine_match.order.channel.value,
                "proof_source": "engine",
            }
            if automatic_order_proven and engine_match is not None and engine_match.order is not None
            else None
        )
        material = {
            "schema_version": "monthly-close-ota-appeal-candidate-v3",
            "issue_id": issue.issue_id,
            "issue_key": issue.issue_key,
            "platform_namespace": later_batch.platform,
            "platform_order_ref": _ota_private_ref(diff.platform_order_id),
            "later_cycle_id": later_cycle.cycle_id,
            "later_billing_month": later_cycle.billing_month,
            "later_document_id": later_document.document_id,
            "later_document_sha256": later_document.sha256,
            "later_batch_id": later_batch.batch_id,
            "later_batch_bill_month": later_batch.bill_month,
            "later_batch_status": later_batch.status,
            "later_batch_fingerprint": later_fingerprint,
            "later_input_set_hash": later_input_hash,
            "source_row": source_row,
            "order_proof": order_proof,
            "settled_amount": _ota_money(settled_amount),
            "short_paid": short_paid,
            "actionable": bool(
                exact
                and order_proven
                and not competing_appeals
                and not short_paid
                and not consumed_elsewhere
            ),
            "reason_code": reason_code,
            "recovery_kind": recovery_kind,
            "action": action,
            "identity_choices": identity_choices,
            "original": {
                "cycle_id": original_cycle.cycle_id,
                "billing_month": original_cycle.billing_month,
                "batch_id": original_batch.batch_id,
                "batch_status": original_batch.status,
                "diff_id": diff.diff_id,
                "proposal_id": proposal.proposal_id,
                "attempt_id": attempt.attempt_id,
                "verification_id": verification.verification_id,
                "approved_evidence_hash": issue.last_seen_evidence_hash,
                "verification_evidence_hash": verification.evidence_hash,
                "active_input_set_hash": original_input_hash,
                "command_hash": _ota_digest(matching_commands[0]),
            },
        }
        candidate_hash = _ota_digest(material)
        candidates.append(
            {
                **material,
                "candidate_id": f"OASC-{candidate_hash[:19]}",
                "candidate_hash": candidate_hash,
            }
        )
    if persist:
        later_batch.mapping = {
            **(later_batch.mapping or {}),
            "appeal_settlement_candidates": candidates,
        }
        await db.flush()
    return candidates


async def list_ota_appeal_settlement_candidates(
    db: AsyncSession, cycle_id: str
) -> list[dict[str, Any]]:
    from app.models.monthly_close_control import MonthlyCloseIssueInstance

    active_rows = list(
        await db.execute(
            select(ReconBatch, MonthlyCloseDocument)
            .join(
                MonthlyCloseDocument,
                MonthlyCloseDocument.engine_id == ReconBatch.batch_id,
            )
            .where(
                MonthlyCloseDocument.cycle_id == cycle_id,
                MonthlyCloseDocument.source_type == "ota_statement",
                MonthlyCloseDocument.engine_type == "billing_recon",
                MonthlyCloseDocument.is_active.is_(True),
            )
            .order_by(ReconBatch.batch_id, MonthlyCloseDocument.document_id)
        )
    )
    items: list[dict[str, Any]] = []
    seen: set[str] = set()
    for batch, document in active_rows:
        if "appeal_settlement_candidates" not in (batch.mapping or {}):
            continue
        document_with_content = await db.scalar(
            select(MonthlyCloseDocument)
            .options(undefer(MonthlyCloseDocument.content))
            .where(MonthlyCloseDocument.document_id == document.document_id)
        )
        if document_with_content is None:
            continue
        current_candidates = await stage_ota_appeal_settlement_candidates(
            db, await db.get(MonthlyCloseCycle, cycle_id), document_with_content, batch
        )
        for candidate in current_candidates:
            if not isinstance(candidate, dict):
                continue
            candidate_id = str(candidate.get("candidate_id", ""))
            if not candidate_id or candidate_id in seen:
                continue
            seen.add(candidate_id)
            items.append(candidate)
    archived_rows = list(
        await db.execute(
            select(ReconBatch, MonthlyCloseDocument)
            .join(
                MonthlyCloseDocument,
                MonthlyCloseDocument.engine_id == ReconBatch.batch_id,
            )
            .where(
                MonthlyCloseDocument.cycle_id == cycle_id,
                MonthlyCloseDocument.source_type == "ota_statement",
                MonthlyCloseDocument.engine_type == "billing_recon",
                MonthlyCloseDocument.is_active.is_(False),
            )
            .order_by(ReconBatch.batch_id, MonthlyCloseDocument.document_id)
        )
    )
    active_issue_ids = {str(item.get("issue_id", "")) for item in items}
    for batch, _document in archived_rows:
        for stored in (batch.mapping or {}).get("appeal_settlement_candidates", []):
            if not isinstance(stored, dict):
                continue
            issue_id = str(stored.get("issue_id", ""))
            if not issue_id or issue_id in active_issue_ids:
                continue
            material = {
                **{
                    key: value
                    for key, value in stored.items()
                    if key not in {"candidate_id", "candidate_hash"}
                },
                "schema_version": "monthly-close-ota-appeal-candidate-v3",
                "actionable": False,
                "reason_code": "evidence_stale",
                "recovery_kind": "refresh_history",
                "action": "该到账证据已归档，请刷新并查看历史记录",
                "identity_choices": [],
            }
            candidate_hash = _ota_digest(material)
            items.append(
                {
                    **material,
                    "candidate_id": f"OASC-{candidate_hash[:19]}",
                    "candidate_hash": candidate_hash,
                }
            )
            active_issue_ids.add(issue_id)
    issue_ids = [str(item.get("issue_id", "")) for item in items]
    open_issue_ids = set(
        await db.scalars(
            select(MonthlyCloseIssueInstance.issue_id).where(
                MonthlyCloseIssueInstance.issue_id.in_(issue_ids or ["__none__"]),
                MonthlyCloseIssueInstance.status == "waiting_information",
            )
        )
    )
    return [item for item in items if item.get("issue_id") in open_issue_ids]


def _ota_appeal_adjudication_before(
    candidate: dict[str, Any],
    *,
    action: str,
    reason: str,
    identity_choice_id: str | None,
) -> dict[str, Any]:
    original = candidate.get("original") if isinstance(candidate.get("original"), dict) else {}
    selected_identity = next(
        (
            item
            for item in candidate.get("identity_choices", [])
            if isinstance(item, dict) and item.get("choice_id") == identity_choice_id
        ),
        None,
    )
    return {
        "schema_version": "monthly-close-ota-appeal-adjudication-command-v1",
        "action": action,
        "reason": reason,
        "candidate_id": candidate.get("candidate_id"),
        "candidate_hash": candidate.get("candidate_hash"),
        "reason_code": candidate.get("reason_code"),
        "recovery_kind": candidate.get("recovery_kind"),
        "issue_id": candidate.get("issue_id"),
        "issue_key": candidate.get("issue_key"),
        "original_cycle_id": original.get("cycle_id"),
        "original_billing_month": original.get("billing_month"),
        "original_batch_id": original.get("batch_id"),
        "original_batch_status": original.get("batch_status"),
        "original_diff_id": original.get("diff_id"),
        "original_proposal_id": original.get("proposal_id"),
        "original_attempt_id": original.get("attempt_id"),
        "original_verification_id": original.get("verification_id"),
        "original_verification_evidence_hash": original.get(
            "verification_evidence_hash"
        ),
        "original_active_input_set_hash": original.get("active_input_set_hash"),
        "original_command_hash": original.get("command_hash"),
        "approved_evidence_hash": original.get("approved_evidence_hash"),
        "later_cycle_id": candidate.get("later_cycle_id"),
        "later_billing_month": candidate.get("later_billing_month"),
        "later_document_id": candidate.get("later_document_id"),
        "later_document_sha256": candidate.get("later_document_sha256"),
        "later_batch_id": candidate.get("later_batch_id"),
        "later_batch_status": candidate.get("later_batch_status"),
        "later_batch_fingerprint": candidate.get("later_batch_fingerprint"),
        "later_input_set_hash": candidate.get("later_input_set_hash"),
        "source_row": candidate.get("source_row"),
        "selected_identity": selected_identity,
        "target": {
            "status": ReconDiffStatus.appeal_pending.value,
            "dismissal_reason": None,
            "identity_proof": None,
        },
    }


def _ota_appeal_identity_proof(before: dict[str, Any]) -> dict[str, Any]:
    identity = before.get("selected_identity") or {}
    return {
        "schema_version": "monthly-close-ota-appeal-identity-v1",
        "issue_id": before["issue_id"],
        "issue_key": before["issue_key"],
        "issue_evidence_hash": before["approved_evidence_hash"],
        "order_id_hash": identity.get("order_ref"),
        "order_channel": identity.get("channel"),
        "order_version": identity.get("order_version"),
        "source_row_hash": (before.get("source_row") or {}).get("row_hash"),
        "later_cycle_id": before["later_cycle_id"],
        "later_document_id": before["later_document_id"],
        "later_document_sha256": before["later_document_sha256"],
        "later_batch_id": before["later_batch_id"],
        "later_batch_fingerprint": before["later_batch_fingerprint"],
    }


def _ota_appeal_adjudication_after(before: dict[str, Any]) -> dict[str, Any]:
    after = json.loads(json.dumps(before, ensure_ascii=False))
    if before["action"] == "withdraw":
        after["target"] = {
            "status": ReconDiffStatus.dismissed.value,
            "dismissal_reason": before["reason"],
            "identity_proof": None,
        }
    else:
        after["target"] = {
            "status": ReconDiffStatus.appeal_pending.value,
            "dismissal_reason": None,
            "identity_proof": _ota_appeal_identity_proof(before),
        }
    return after


def _ota_appeal_adjudication_audit_identity(
    command: dict[str, Any],
) -> dict[str, Any]:
    """Derive the only valid adjudication write result from canonical input."""
    before = command.get("before") if isinstance(command.get("before"), dict) else {}
    action = before.get("action")
    status = (
        ReconDiffStatus.dismissed.value
        if action == "withdraw"
        else ReconDiffStatus.appeal_pending.value
    )
    diff_id = before.get("original_diff_id")
    return {
        "action": "monthly_close.ota_appeal.adjudication.execute",
        "resource_type": "recon_diff",
        "resource_id": diff_id,
        "result": {"diff_id": diff_id, "status": status},
    }


async def _current_ota_appeal_adjudication_before(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    command: dict[str, Any],
) -> dict[str, Any]:
    from app.services.monthly_close.control import MonthlyCloseControlError

    approved = command.get("before") if isinstance(command.get("before"), dict) else {}
    later_cycle_id = str(approved.get("later_cycle_id", ""))
    candidate_id = str(approved.get("candidate_id", ""))
    if approved.get("original_cycle_id") != cycle.cycle_id or not later_cycle_id or not candidate_id:
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_stale", "申诉裁决的月份或到账证据已经变化"
        )
    candidates = await list_ota_appeal_settlement_candidates(db, later_cycle_id)
    candidate = next(
        (item for item in candidates if item.get("candidate_id") == candidate_id), None
    )
    if candidate is None or candidate.get("issue_id") != approved.get("issue_id"):
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_stale", "申诉裁决证据已经变化，请重新生成方案"
        )
    rebuilt = _ota_appeal_adjudication_before(
        candidate,
        action=str(approved.get("action", "")),
        reason=str(approved.get("reason", "")),
        identity_choice_id=(
            (approved.get("selected_identity") or {}).get("choice_id")
            if isinstance(approved.get("selected_identity"), dict)
            else None
        ),
    )
    if rebuilt != approved:
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_stale", "申诉裁决证据已经变化，请重新生成方案"
        )
    return rebuilt


async def load_ota_appeal_adjudication_context(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    commands: Sequence[dict[str, Any]],
):
    from app.services.monthly_close.control import ProposalFreshnessContext

    if len(commands) != 1:
        raise ValueError("OTA appeal adjudication requires exactly one command")
    before = await _current_ota_appeal_adjudication_before(db, cycle, commands[0])
    subject_id = str(commands[0]["subject_id"])
    evidence_hash = _ota_digest(before)
    return ProposalFreshnessContext(
        evidence_hash=evidence_hash,
        subject_versions={subject_id: _ota_digest(before)},
        active_input_set_hash=str(before["later_input_set_hash"]),
        mapping_versions={
            "later_batch_fingerprint": before["later_batch_fingerprint"],
            "later_document_sha256": before["later_document_sha256"],
        },
        ruleset_version=_OTA_APPEAL_ADJUDICATION_RULESET_VERSION,
        calculation_version=_OTA_APPEAL_ADJUDICATION_CALCULATION_VERSION,
        configuration_snapshot_hash=_ota_digest(
            {
                "action": before["action"],
                "later_cycle_id": before["later_cycle_id"],
                "original_cycle_id": before["original_cycle_id"],
            }
        ),
        control_version=cycle.control_version,
    )


async def build_ota_appeal_adjudication_proposal(
    db: AsyncSession,
    later_cycle_id: str,
    candidate_id: str,
    action: str,
    reason: str,
    actor: Any,
    *,
    identity_choice_id: str | None = None,
    request_id: str,
):
    from app.services.monthly_close.control import MonthlyCloseControlError, create_proposal

    reason = (reason or "").strip()
    if not reason:
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_reason_required", "申诉裁决必须填写核对原因"
        )
    if action not in {"withdraw", "confirm_identity"}:
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_action_invalid", "申诉裁决动作无效"
        )
    candidates = await list_ota_appeal_settlement_candidates(db, later_cycle_id)
    candidate = next(
        (item for item in candidates if item.get("candidate_id") == candidate_id), None
    )
    if candidate is None:
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_stale", "到账证据已经变化，请刷新后重试"
        )
    allowed_recovery = {"adjudicate_appeals", "confirm_identity"}
    if candidate.get("recovery_kind") not in allowed_recovery:
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_not_allowed", "该问题不能通过申诉裁决处理"
        )
    selected = next(
        (
            item
            for item in candidate.get("identity_choices", [])
            if isinstance(item, dict) and item.get("choice_id") == identity_choice_id
        ),
        None,
    )
    if action == "confirm_identity" and (
        selected is None or not selected.get("is_current_appeal")
    ):
        raise MonthlyCloseControlError(
            "ota_appeal_identity_invalid", "必须选择当前申诉对应的准确订单和渠道"
        )
    if action == "withdraw" and identity_choice_id is not None:
        raise MonthlyCloseControlError(
            "ota_appeal_identity_invalid", "撤销错误申诉时不能提交订单选择"
        )
    original = candidate.get("original") or {}
    cycle = await db.get(MonthlyCloseCycle, original.get("cycle_id"))
    if cycle is None or cycle.write_control_owner != "assistant":
        raise MonthlyCloseControlError(
            "ota_control_owner_changed", "OTA写入控制权已经变化，请重新生成方案"
        )
    before = _ota_appeal_adjudication_before(
        candidate,
        action=action,
        reason=reason,
        identity_choice_id=identity_choice_id,
    )
    after = _ota_appeal_adjudication_after(before)
    subject_id = f"ota-appeal:{candidate['issue_id']}"
    decision_digest = _ota_digest(
        {
            "action": action,
            "candidate_hash": candidate["candidate_hash"],
            "identity_choice_id": identity_choice_id,
            "reason": reason,
        }
    )
    evidence_refs = [
        {
            "kind": "appeal_identity" if action == "confirm_identity" else "issue",
            "issue_id": candidate["issue_id"],
            "evidence_hash": original["approved_evidence_hash"],
        },
        {
            "kind": "ota_statement_row",
            "document_id": candidate["later_document_id"],
            "document_sha256": candidate["later_document_sha256"],
            "batch_id": candidate["later_batch_id"],
            "batch_fingerprint": candidate["later_batch_fingerprint"],
            "source_row_hash": (candidate.get("source_row") or {}).get("row_hash"),
        },
    ]
    command = {
        "command_type": "ota_appeal_adjudication",
        "subject_id": subject_id,
        "before": before,
        "after": after,
        "amount_impact": "0.00",
        "evidence_refs": evidence_refs,
        "business_idempotency_key": f"ota-appeal:{candidate['issue_id']}:{decision_digest[:24]}",
    }
    context = await load_ota_appeal_adjudication_context(db, cycle, [command])
    proposal = await create_proposal(
        db,
        cycle,
        actor,
        [command],
        evidence_hash=context.evidence_hash,
        request_id=request_id,
        proposal_type="ota_appeal_adjudication",
        server_owned_commands=True,
        impact_snapshot={
            "change_count": 1,
            "changes": [{"before": before, "after": after, "amount_impact": "0.00"}],
            "required_approver": "管理员",
            "risk": "仅裁决申诉状态或订单渠道身份，不修改订单或费用金额",
            "total_amount": "0.00",
            "verification": "执行后重新读取原申诉、到账行、裁决证据和审计记录",
        },
    )
    return proposal


async def _lock_ota_appeal_adjudication_evidence(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
) -> dict[str, Any]:
    """Lock and authoritatively re-read every mutable adjudication fact."""
    from app.models.monthly_close_control import (
        MonthlyCloseExecutionAttempt,
        MonthlyCloseIssueInstance,
        MonthlyCloseProposal,
        MonthlyCloseVerification,
    )
    from app.services.monthly_close.control import (
        MonthlyCloseControlError,
        verified_execution_lineage_is_valid,
    )

    approved = command.get("before") if isinstance(command.get("before"), dict) else {}
    original_cycle_id, locked_cycles = await _lock_ota_settlement_cycles(
        db,
        str(approved.get("later_cycle_id", "")),
        str(approved.get("issue_id", "")),
    )
    later_cycle = locked_cycles.get(str(approved.get("later_cycle_id", "")))
    if (
        original_cycle_id != cycle.cycle_id
        or cycle.cycle_id not in locked_cycles
        or later_cycle is None
        or locked_cycles[cycle.cycle_id].write_control_owner != "assistant"
        or later_cycle.write_control_owner != "assistant"
    ):
        raise MonthlyCloseControlError(
            "ota_control_owner_changed", "OTA写入控制权已经变化，请重新生成方案"
        )
    later_document = await db.scalar(
        select(MonthlyCloseDocument)
        .options(undefer(MonthlyCloseDocument.content))
        .where(
            MonthlyCloseDocument.document_id
            == str(approved.get("later_document_id", "")),
            MonthlyCloseDocument.cycle_id == later_cycle.cycle_id,
            MonthlyCloseDocument.source_type == "ota_statement",
            MonthlyCloseDocument.engine_type == "billing_recon",
            MonthlyCloseDocument.processing_status == "processed",
            MonthlyCloseDocument.engine_id
            == str(approved.get("later_batch_id", "")),
            MonthlyCloseDocument.is_active.is_(True),
        )
        .with_for_update(of=MonthlyCloseDocument)
        .execution_options(populate_existing=True)
    )
    batch_rows = list(
        await db.scalars(
            select(ReconBatch)
            .where(
                ReconBatch.batch_id.in_(
                    [
                        str(approved.get("original_batch_id", "")),
                        str(approved.get("later_batch_id", "")),
                    ]
                )
            )
            .order_by(ReconBatch.batch_id)
            .with_for_update(of=ReconBatch)
            .execution_options(populate_existing=True)
        )
    )
    batches = {item.batch_id: item for item in batch_rows}
    original_batch = batches.get(str(approved.get("original_batch_id", "")))
    later_batch = batches.get(str(approved.get("later_batch_id", "")))
    issue = await db.scalar(
        select(MonthlyCloseIssueInstance)
        .where(
            MonthlyCloseIssueInstance.cycle_id == cycle.cycle_id,
            MonthlyCloseIssueInstance.issue_id == approved.get("issue_id"),
            MonthlyCloseIssueInstance.issue_key == approved.get("issue_key"),
            MonthlyCloseIssueInstance.last_seen_evidence_hash
            == approved.get("approved_evidence_hash"),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    diff = await db.scalar(
        select(ReconDiff)
        .where(
            ReconDiff.batch_id == approved.get("original_batch_id"),
            ReconDiff.diff_id == approved.get("original_diff_id"),
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    current_order = (
        await db.scalar(
            select(Order)
            .where(Order.order_id == diff.order_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if diff is not None and diff.order_id
        else None
    )
    original_proposal = await db.scalar(
        select(MonthlyCloseProposal)
        .where(
            MonthlyCloseProposal.cycle_id == cycle.cycle_id,
            MonthlyCloseProposal.proposal_id == approved.get("original_proposal_id"),
            MonthlyCloseProposal.proposal_type == "ota_reconciliation",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    original_attempt = await db.scalar(
        select(MonthlyCloseExecutionAttempt)
        .where(
            MonthlyCloseExecutionAttempt.cycle_id == cycle.cycle_id,
            MonthlyCloseExecutionAttempt.attempt_id == approved.get("original_attempt_id"),
            MonthlyCloseExecutionAttempt.proposal_id == approved.get("original_proposal_id"),
            MonthlyCloseExecutionAttempt.status == "verified",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    original_verification = await db.scalar(
        select(MonthlyCloseVerification)
        .where(
            MonthlyCloseVerification.cycle_id == cycle.cycle_id,
            MonthlyCloseVerification.verification_id
            == approved.get("original_verification_id"),
            MonthlyCloseVerification.attempt_id == approved.get("original_attempt_id"),
            MonthlyCloseVerification.status == "passed",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    lineage_valid = bool(
        original_proposal is not None
        and original_attempt is not None
        and original_verification is not None
        and await verified_execution_lineage_is_valid(
            db,
            proposal_id=original_proposal.proposal_id,
            attempt_id=original_attempt.attempt_id,
            verification_id=original_verification.verification_id,
            cycle_id=cycle.cycle_id,
            proposal_type="ota_reconciliation",
            lock=True,
        )
    )
    if (
        later_document is None
        or original_batch is None
        or later_batch is None
        or len(batches) != 2
        or issue is None
        or diff is None
        or current_order is None
        or original_proposal is None
        or original_attempt is None
        or original_verification is None
        or not lineage_valid
        or original_batch.status != approved.get("original_batch_status")
        or original_verification.evidence_hash
        != approved.get("original_verification_evidence_hash")
        or later_document.sha256 != approved.get("later_document_sha256")
        or hashlib.sha256(later_document.content).hexdigest()
        != later_document.sha256
        or later_batch.bill_month != approved.get("later_billing_month")
        or later_batch.status != approved.get("later_batch_status")
        or (later_batch.mapping or {}).get("upload_fingerprint")
        != approved.get("later_batch_fingerprint")
    ):
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_stale", "申诉裁决到账或原申诉证据已经变化"
        )
    return {
        "later_cycle": later_cycle,
        "later_document": later_document,
        "later_batch": later_batch,
        "issue": issue,
        "diff": diff,
        "order": current_order,
    }


async def _rebuild_ota_appeal_adjudication_before(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    *,
    replay_executed_action: bool,
) -> tuple[dict[str, Any], dict[str, Any]]:
    from app.services.monthly_close.control import MonthlyCloseControlError

    approved = command.get("before") if isinstance(command.get("before"), dict) else {}
    locked = await _lock_ota_appeal_adjudication_evidence(
        db, cycle, proposal, command
    )
    candidates = await stage_ota_appeal_settlement_candidates(
        db,
        locked["later_cycle"],
        locked["later_document"],
        locked["later_batch"],
        replay_diff_id=(
            str(approved.get("original_diff_id", ""))
            if replay_executed_action
            else None
        ),
        ignored_adjudication_proposal_id=(
            proposal.proposal_id if replay_executed_action else None
        ),
        persist=False,
    )
    candidate = next(
        (
            item
            for item in candidates
            if item.get("candidate_id") == approved.get("candidate_id")
            and item.get("issue_id") == approved.get("issue_id")
        ),
        None,
    )
    if candidate is None:
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_stale", "申诉裁决证据已经变化，请重新生成方案"
        )
    rebuilt = _ota_appeal_adjudication_before(
        candidate,
        action=str(approved.get("action", "")),
        reason=str(approved.get("reason", "")),
        identity_choice_id=(
            (approved.get("selected_identity") or {}).get("choice_id")
            if isinstance(approved.get("selected_identity"), dict)
            else None
        ),
    )
    if rebuilt != approved:
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_stale", "申诉裁决证据已经变化，请重新生成方案"
        )
    return rebuilt, locked


async def execute_ota_appeal_adjudication_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    actor: Any,
    request_id: str,
) -> dict[str, Any]:
    from app.services.monthly_close.control import MonthlyCloseControlError

    before, locked = await _rebuild_ota_appeal_adjudication_before(
        db,
        cycle,
        proposal,
        command,
        replay_executed_action=False,
    )
    if _ota_appeal_adjudication_after(before) != command.get("after"):
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_not_server_owned", "申诉裁决命令与当前确定性计划不一致"
        )
    issue = locked["issue"]
    diff = locked["diff"]
    if issue is None or diff is None or diff.status != ReconDiffStatus.appeal_pending:
        raise MonthlyCloseControlError(
            "ota_appeal_adjudication_stale", "申诉状态已经变化，请重新生成方案"
        )
    actor_id = actor.user_id
    if before["action"] == "withdraw":
        diff.status = ReconDiffStatus.dismissed
        diff.resolved_by = actor_id
        diff.resolved_at = datetime.now(timezone.utc)
        diff.detail = {
            **(diff.detail or {}),
            "dismissal_reason": before["reason"],
            "appeal_adjudication_proposal_id": proposal.proposal_id,
        }
    else:
        proof = command["after"]["target"]["identity_proof"]
        diff.detail = {
            **(diff.detail or {}),
            "appeal_identity_adjudication": proof,
            "appeal_identity_adjudication_proposal_id": proposal.proposal_id,
        }
    await log_action_tx(
        db,
        actor_id,
        "monthly_close.ota_appeal.adjudication.execute",
        "recon_diff",
        diff.diff_id,
        before_data=command["before"],
        after_data={
            "approved_after": command["after"],
            "business_idempotency_key": command["business_idempotency_key"],
            "proposal_id": proposal.proposal_id,
            "request_id": request_id,
        },
    )
    await db.flush()
    audit_id = await db.scalar(
        select(func.max(AuditLog.log_id)).where(
            AuditLog.action == "monthly_close.ota_appeal.adjudication.execute",
            AuditLog.resource_id == diff.diff_id,
        )
    )
    return {
        "result": {"diff_id": diff.diff_id, "status": diff.status.value},
        "audit_refs": [str(audit_id)] if audit_id is not None else [],
        "execution_audits": (
            [
                {
                    "audit_ref": str(audit_id),
                    **_ota_appeal_adjudication_audit_identity(command),
                }
            ]
            if audit_id is not None
            else []
        ),
    }


async def verify_ota_appeal_adjudication_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    attempt: Any,
) -> dict[str, Any]:
    from app.services.monthly_close.control import (
        authoritative_execution_evidence_is_valid,
    )

    before, locked = await _rebuild_ota_appeal_adjudication_before(
        db,
        cycle,
        proposal,
        command,
        replay_executed_action=True,
    )
    diff = locked["diff"]
    issue = locked["issue"]
    audit_present = await authoritative_execution_evidence_is_valid(
        db,
        proposal=proposal,
        attempt=attempt,
        expected_audit_identity=_ota_appeal_adjudication_audit_identity,
        lock=True,
    )
    stored_proposal_id = (
        (diff.detail or {}).get("appeal_adjudication_proposal_id")
        if diff is not None and before["action"] == "withdraw"
        else (
            (diff.detail or {}).get("appeal_identity_adjudication_proposal_id")
            if diff is not None
            else None
        )
    )
    proposal_binding_present = stored_proposal_id == proposal.proposal_id
    actual = _ota_appeal_adjudication_after(before)
    if before["action"] == "withdraw":
        actual["reason"] = (diff.detail or {}).get("dismissal_reason")
        actual["target"] = {
            "status": diff.status.value,
            "dismissal_reason": (diff.detail or {}).get("dismissal_reason"),
            "identity_proof": None,
        }
    else:
        actual["target"] = {
            "status": diff.status.value,
            "dismissal_reason": None,
            "identity_proof": (diff.detail or {}).get(
                "appeal_identity_adjudication"
            ),
        }
    applied = (
        actual == command["after"]
        and audit_present
        and proposal_binding_present
    )
    issue_matches = bool(
        before["action"] == "withdraw"
        and issue.status == "waiting_information"
        and issue.resolution_proposal_id == before["original_proposal_id"]
        and issue.verification_id == before["original_verification_id"]
        and issue.last_seen_evidence_hash == before["approved_evidence_hash"]
    )
    if applied and issue_matches:
        issue.status = "proposed_fix"
        issue.resolution_proposal_id = proposal.proposal_id
        issue.verification_id = None
    verification_hash = _ota_digest(
        {
            "actual": actual,
            "attempt_id": attempt.attempt_id,
            "audit_present": audit_present,
            "issue_matches": issue_matches,
            "proposal_binding_present": proposal_binding_present,
            "rebuilt_before": before,
        }
    )
    expected_version = proposal.subject_versions.get(command["subject_id"])
    return {
        "outcome": "applied" if applied and (before["action"] != "withdraw" or issue_matches) else "inconclusive",
        "business_idempotency_key": command["business_idempotency_key"],
        "subject": {
            "subject_id": command["subject_id"],
            "expected_version": expected_version,
            "actual_version": _ota_digest(before),
        },
        "target": {
            "expected_before": command["before"],
            "expected_after": command["after"],
            "actual": actual,
        },
        "amount": {"expected": "0.00", "actual": "0.00"},
        "audit": {
            "present": audit_present,
            "request_id": attempt.request_id,
            "business_idempotency_key": command["business_idempotency_key"],
            "expected_before": command["before"],
            "expected_after": command["after"],
        },
        "idempotency": {
            "present": audit_present,
            "request_id": attempt.request_id,
            "business_idempotency_key": command["business_idempotency_key"],
        },
        "evidence": {
            "approved_evidence_hash": proposal.evidence_hash,
            "verification_evidence_hash": verification_hash,
        },
        "issues": (
            [
                {
                    "issue_id": issue.issue_id,
                    "approved_evidence_hash": before["approved_evidence_hash"],
                    "actual_evidence_hash": issue.last_seen_evidence_hash,
                    "resolution": "resolve",
                }
            ]
            if applied and issue_matches
            else []
        ),
    }


async def _lock_ota_settlement_cycles(
    db: AsyncSession, later_cycle_id: str, issue_id: str
) -> tuple[str | None, dict[str, MonthlyCloseCycle]]:
    """Resolve only the cycle identity, then take the shared sorted lock order."""
    from app.models.monthly_close_control import MonthlyCloseIssueInstance

    original_cycle_id = await db.scalar(
        select(MonthlyCloseIssueInstance.cycle_id).where(
            MonthlyCloseIssueInstance.issue_id == issue_id
        )
    )
    cycle_ids = sorted(
        {value for value in (later_cycle_id, original_cycle_id) if value}
    )
    cycles = list(
        await db.scalars(
            select(MonthlyCloseCycle)
            .where(MonthlyCloseCycle.cycle_id.in_(cycle_ids or ["__none__"]))
            .order_by(MonthlyCloseCycle.billing_month, MonthlyCloseCycle.cycle_id)
            .with_for_update(of=MonthlyCloseCycle)
            .execution_options(populate_existing=True)
        )
    )
    return original_cycle_id, {cycle.cycle_id: cycle for cycle in cycles}


async def reconcile_settled_ota_issue(
    db: AsyncSession,
    cycle_id: str,
    issue_id: str,
    actor: Any,
    *,
    request_id: str,
):
    """Apply one server-built later-statement candidate without committing."""
    from app.models.monthly_close_control import (
        MonthlyCloseExecutionAttempt,
        MonthlyCloseIssueInstance,
        MonthlyCloseOtaSettlementConsumption,
        MonthlyCloseProposal,
        MonthlyCloseVerification,
    )
    from app.services.monthly_close.control import MonthlyCloseControlError

    invalid = MonthlyCloseControlError(
        "ota_appeal_lineage_invalid",
        "该申诉的批准、执行或复核证据不完整，请重新核对",
    )
    original_cycle_id_hint = await db.scalar(
        select(MonthlyCloseIssueInstance.cycle_id).where(
            MonthlyCloseIssueInstance.issue_id == issue_id
        )
    )
    month_rows = list(
        await db.scalars(
            select(MonthlyCloseCycle.billing_month).where(
                MonthlyCloseCycle.cycle_id.in_(
                    [
                        value
                        for value in (cycle_id, original_cycle_id_hint)
                        if value is not None
                    ]
                    or ["__none__"]
                )
            )
        )
    )
    for billing_month in sorted(set(month_rows)):
        await acquire_month_financial_lock(db, billing_month)
    original_cycle_id, by_cycle = await _lock_ota_settlement_cycles(
        db, cycle_id, issue_id
    )
    original_cycle = by_cycle.get(original_cycle_id or "")
    later_cycle = by_cycle.get(cycle_id)
    if (
        original_cycle is None
        or later_cycle is None
        or original_cycle.write_control_owner != "assistant"
        or later_cycle.write_control_owner != "assistant"
    ):
        raise invalid

    # Every path that archives or replaces evidence takes cycle -> document.
    # Re-read all active source documents only after both cycle locks; objects
    # seen before this point are never used as authorization or evidence.
    locked_documents = list(
        await db.scalars(
            select(MonthlyCloseDocument)
            .options(undefer(MonthlyCloseDocument.content))
            .where(
                MonthlyCloseDocument.cycle_id.in_(
                    [original_cycle.cycle_id, later_cycle.cycle_id]
                ),
                MonthlyCloseDocument.source_type == "ota_statement",
                MonthlyCloseDocument.engine_type == "billing_recon",
                MonthlyCloseDocument.is_active.is_(True),
            )
            .order_by(MonthlyCloseDocument.cycle_id, MonthlyCloseDocument.document_id)
            .with_for_update(of=MonthlyCloseDocument)
            .execution_options(populate_existing=True)
        )
    )
    original_documents = [
        document
        for document in locked_documents
        if document.cycle_id == original_cycle.cycle_id
    ]
    later_documents = [
        document
        for document in locked_documents
        if document.cycle_id == later_cycle.cycle_id
    ]

    issue = await db.scalar(
        select(MonthlyCloseIssueInstance)
        .where(
            MonthlyCloseIssueInstance.cycle_id == original_cycle.cycle_id,
            MonthlyCloseIssueInstance.issue_id == issue_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if issue is None or issue.adapter_type != "ota_reconciliation":
        raise invalid
    proposal = await db.scalar(
        select(MonthlyCloseProposal)
        .where(
            MonthlyCloseProposal.cycle_id == original_cycle.cycle_id,
            MonthlyCloseProposal.proposal_id == issue.resolution_proposal_id,
            MonthlyCloseProposal.proposal_type == "ota_reconciliation",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    verification = await db.scalar(
        select(MonthlyCloseVerification)
        .where(
            MonthlyCloseVerification.cycle_id == original_cycle.cycle_id,
            MonthlyCloseVerification.verification_id == issue.verification_id,
            MonthlyCloseVerification.status == "passed",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    attempt = await db.scalar(
        select(MonthlyCloseExecutionAttempt)
        .where(
            MonthlyCloseExecutionAttempt.cycle_id == original_cycle.cycle_id,
            MonthlyCloseExecutionAttempt.proposal_id == issue.resolution_proposal_id,
            MonthlyCloseExecutionAttempt.attempt_id
            == (verification.attempt_id if verification is not None else ""),
            MonthlyCloseExecutionAttempt.status == "verified",
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    commands = (
        proposal.canonical_payload.get("commands", [])
        if proposal is not None and isinstance(proposal.canonical_payload, dict)
        else []
    )
    matching_commands = [
        command
        for command in commands
        if isinstance(command, dict) and command.get("subject_id") == issue.issue_key
    ]
    if proposal is None or verification is None or attempt is None or len(matching_commands) != 1:
        raise invalid
    command = matching_commands[0]
    before = command.get("before") if isinstance(command.get("before"), dict) else {}
    decision = before.get("decision") if isinstance(before.get("decision"), dict) else {}
    original_batch_id = str(before.get("batch_id", ""))
    diff_id = str(before.get("diff_id", ""))
    if (
        decision.get("kind") != "action"
        or decision.get("action") != "appeal"
        or not original_batch_id
        or not diff_id
    ):
        raise invalid

    later_batch_ids = {
        document.engine_id for document in later_documents if document.engine_id
    }
    batch_ids = sorted({original_batch_id, *later_batch_ids})
    locked_batches = list(
        await db.scalars(
            select(ReconBatch)
            .where(ReconBatch.batch_id.in_(batch_ids))
            .order_by(ReconBatch.batch_id)
            .with_for_update(of=ReconBatch)
            .execution_options(populate_existing=True)
        )
    )
    batches = {item.batch_id: item for item in locked_batches}
    original_batch = batches.get(original_batch_id)
    diff = await db.scalar(
        select(ReconDiff)
        .where(
            ReconDiff.diff_id == diff_id,
            ReconDiff.batch_id == original_batch_id,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )

    if issue.status == "resolved":
        existing_consumption = await db.scalar(
            select(MonthlyCloseOtaSettlementConsumption)
            .where(
                MonthlyCloseOtaSettlementConsumption.original_issue_id
                == issue.issue_id
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        consumed_document = next(
            (
                item
                for item in later_documents
                if existing_consumption is not None
                and item.document_id == existing_consumption.later_document_id
            ),
            None,
        )
        consumed_batch = (
            batches.get(existing_consumption.later_batch_id)
            if existing_consumption is not None
            else None
        )
        if (
            existing_consumption is None
            or diff is None
            or diff.status != ReconDiffStatus.appeal_settled
            or issue.last_seen_evidence_hash
            != existing_consumption.settlement_evidence_hash
            or (diff.detail or {}).get("settlement_evidence_hash")
            != existing_consumption.settlement_evidence_hash
            or existing_consumption.original_cycle_id != original_cycle.cycle_id
            or existing_consumption.original_batch_id != original_batch_id
            or existing_consumption.original_diff_id != diff.diff_id
            or existing_consumption.proposal_id != proposal.proposal_id
            or existing_consumption.attempt_id != attempt.attempt_id
            or existing_consumption.verification_id != verification.verification_id
            or consumed_document is None
            or not consumed_document.is_active
            or consumed_document.sha256
            != existing_consumption.later_document_sha256
            or hashlib.sha256(consumed_document.content).hexdigest()
            != consumed_document.sha256
            or consumed_batch is None
            or (consumed_batch.mapping or {}).get("upload_fingerprint")
            != existing_consumption.later_batch_fingerprint
        ):
            raise invalid
        return issue

    matches: list[tuple[dict[str, Any], ReconBatch, MonthlyCloseDocument]] = []
    for later_document in later_documents:
        later_batch = batches.get(later_document.engine_id or "")
        if later_batch is None:
            continue
        current_candidates = await stage_ota_appeal_settlement_candidates(
            db, later_cycle, later_document, later_batch
        )
        for candidate in current_candidates:
            if isinstance(candidate, dict) and candidate.get("issue_id") == issue_id:
                matches.append((candidate, later_batch, later_document))
    if len(matches) != 1:
        raise invalid
    candidate, later_batch, later_document = matches[0]
    if not candidate.get("actionable"):
        code = (
            "ota_appeal_short_paid"
            if candidate.get("short_paid")
            else "ota_appeal_evidence_ambiguous"
        )
        raise MonthlyCloseControlError(
            code, str(candidate.get("action") or "到账证据不能安全核销")
        )
    original = (
        candidate.get("original")
        if isinstance(candidate.get("original"), dict)
        else {}
    )
    if (
        original_batch is None
        or diff is None
        or issue.status not in {"waiting_information", "resolved"}
        or issue.resolution_proposal_id != original.get("proposal_id")
        or issue.verification_id != original.get("verification_id")
        or diff.diff_class != ReconDiffClass.appeal
        or diff.status not in {ReconDiffStatus.appeal_pending, ReconDiffStatus.appeal_settled}
        or ota_issue_key(original_cycle, original_batch, diff, "appeal") != issue.issue_key
        or _ota_private_ref(diff.platform_order_id) != candidate.get("platform_order_ref")
        or original.get("cycle_id") != original_cycle.cycle_id
        or original.get("batch_id") != original_batch.batch_id
        or original.get("diff_id") != diff.diff_id
        or candidate.get("later_cycle_id") != later_cycle.cycle_id
        or candidate.get("later_billing_month") != later_cycle.billing_month
        or original_batch.platform != candidate.get("platform_namespace")
        or later_batch.platform != candidate.get("platform_namespace")
        or original_batch.bill_month != original_cycle.billing_month
    ):
        raise invalid
    if (
        verification.attempt_id != attempt.attempt_id
        or attempt.attempt_id != original.get("attempt_id")
        or _ota_digest(matching_commands[0]) != original.get("command_hash")
        or verification.evidence_hash != original.get("verification_evidence_hash")
    ):
        raise invalid
    current_original_input_hash = _ota_document_input_hash(
        original_batch.batch_id, original_documents
    )
    current_later_input_hash = _ota_document_input_hash(
        later_batch.batch_id, later_documents
    )
    issue_ref = next(
        (
            ref
            for ref in command.get("evidence_refs", [])
            if isinstance(ref, dict)
            and ref.get("kind") == "issue"
            and ref.get("issue_id") == issue.issue_id
        ),
        None,
    )
    approved_evidence_hash = issue_ref.get("evidence_hash") if issue_ref else None
    if (
        not approved_evidence_hash
        or approved_evidence_hash != original.get("approved_evidence_hash")
        or (issue.status != "resolved" and issue.last_seen_evidence_hash != approved_evidence_hash)
        or current_original_input_hash != proposal.active_input_set_hash
        or current_original_input_hash != original.get("active_input_set_hash")
        or current_later_input_hash != candidate.get("later_input_set_hash")
        or later_document.document_id != candidate.get("later_document_id")
        or not later_document.is_active
        or later_document.sha256 != candidate.get("later_document_sha256")
        or later_batch.batch_id != candidate.get("later_batch_id")
        or later_batch.bill_month != candidate.get("later_batch_bill_month")
        or (later_batch.mapping or {}).get("upload_fingerprint")
        != candidate.get("later_batch_fingerprint")
        or not isinstance(candidate.get("later_batch_fingerprint"), str)
        or not re.fullmatch(r"[0-9a-f]{64}", candidate["later_batch_fingerprint"])
        or candidate.get("later_batch_fingerprint") != later_document.sha256
        or hashlib.sha256(later_document.content).hexdigest() != later_document.sha256
    ):
        raise invalid

    source_row = candidate.get("source_row") if isinstance(candidate.get("source_row"), dict) else {}
    try:
        from app.services.billing_recon.analysis import PlatformScope
        from app.services.billing_recon.engine import (
            channels_for_scope,
            fetch_candidates,
            match_orders,
        )
        from app.services.billing_recon.parser import (
            BillMapping,
            aggregate_orders,
            extract_bill_rows,
            load_workbook_rows,
        )

        sheets, datemode = await asyncio.to_thread(
            load_workbook_rows, later_document.content, later_document.filename
        )
        mapping = BillMapping.model_validate(later_batch.mapping or {})
        parsed_rows = extract_bill_rows(sheets[mapping.sheet], mapping, datemode)
        bill_orders = aggregate_orders(parsed_rows)
        later_scope = PlatformScope(
            str((later_batch.mapping or {}).get("platform_scope"))
        )
        checkouts = [item.checkout for item in bill_orders.values() if item.checkout]
        system_candidates = await fetch_candidates(
            db,
            later_batch.bill_month,
            date_lo=min(checkouts) if checkouts else None,
            date_hi=max(checkouts) if checkouts else None,
            channels=channels_for_scope(later_scope),
        )
        engine_match = match_orders(bill_orders, system_candidates).get(
            diff.platform_order_id or ""
        )
    except Exception as exc:  # noqa: BLE001 - stale/corrupt evidence fails closed
        raise invalid from exc
    exact_rows = [
        row for row in parsed_rows
        if row.order_no == diff.platform_order_id
        and row.source_row_index == source_row.get("row_index")
    ]
    if len(exact_rows) != 1:
        raise invalid
    current_row = _ota_settlement_row_fact(exact_rows[0], diff.platform_order_id)
    candidate_order_proof = (
        candidate.get("order_proof")
        if isinstance(candidate.get("order_proof"), dict)
        else {}
    )
    current_order_proof = None
    if candidate_order_proof.get("match_via") == "adjudicated":
        current_order_proof = await _verified_ota_identity_proof(
            db,
            issue=issue,
            diff=diff,
            source_row=current_row,
            later_cycle=later_cycle,
            later_document=later_document,
            later_batch=later_batch,
        )
    elif (
        engine_match is not None
        and engine_match.via == "exact"
        and engine_match.order is not None
        and engine_match.order.order_id == diff.order_id
        and engine_match.order.channel in channels_for_scope(later_scope)
    ):
        current_order_proof = {
            "match_via": engine_match.via,
            "order_id_hash": _ota_private_ref(engine_match.order.order_id),
            "order_channel": engine_match.order.channel.value,
            "proof_source": "engine",
        }
    settled_amount = Decimal(str(current_row["amount"]))
    short_paid = bool(
        diff.system_amount is not None
        and (diff.system_amount - settled_amount) > Decimal("0.01")
    )
    if (
        current_row != source_row
        or current_order_proof is None
        or current_order_proof != candidate.get("order_proof")
        or short_paid
        or _ota_digest({
            key: value for key, value in candidate.items()
            if key not in {"candidate_id", "candidate_hash"}
        })
        != candidate.get("candidate_hash")
    ):
        if short_paid:
            raise MonthlyCloseControlError(
                "ota_appeal_short_paid", "本次到账少于原申诉金额，请继续向平台追款"
            )
        raise invalid

    competing_count = await db.scalar(
        select(func.count())
        .select_from(ReconDiff)
        .join(ReconBatch, ReconDiff.batch_id == ReconBatch.batch_id)
        .where(
            ReconBatch.platform == later_batch.platform,
            ReconDiff.diff_class == ReconDiffClass.appeal,
            ReconDiff.status == ReconDiffStatus.appeal_pending,
            ReconDiff.platform_order_id == diff.platform_order_id,
        )
    )
    if issue.status != "resolved" and competing_count != 1:
        raise MonthlyCloseControlError(
            "ota_appeal_evidence_ambiguous",
            "同一笔到账匹配多条历史申诉，请先核对并保留唯一申诉",
        )

    settlement_evidence_hash = _ota_digest(
        {
            "schema_version": "monthly-close-ota-appeal-settlement-v2",
            "cycle_id": original_cycle.cycle_id,
            "issue_id": issue.issue_id,
            "issue_key": issue.issue_key,
            "proposal_id": proposal.proposal_id,
            "attempt_id": attempt.attempt_id,
            "verification_id": verification.verification_id,
            "batch_id": original_batch.batch_id,
            "diff_id": diff.diff_id,
            "approved_command_hash": original.get("command_hash"),
            "approved_evidence_hash": approved_evidence_hash,
            "verification_evidence_hash": verification.evidence_hash,
            "original_input_set_hash": current_original_input_hash,
            "later_input_set_hash": current_later_input_hash,
            "candidate_hash": candidate.get("candidate_hash"),
            "later_document_id": later_document.document_id,
            "later_document_sha256": later_document.sha256,
            "later_document_is_active": later_document.is_active,
            "later_batch_id": later_batch.batch_id,
            "later_batch_fingerprint": candidate.get("later_batch_fingerprint"),
            "platform_namespace": later_batch.platform,
            "order_proof": current_order_proof,
            "source_row": current_row,
        }
    )
    row_identity_hash = _ota_digest(
        {
            "schema_version": "monthly-close-ota-row-identity-v1",
            "later_cycle_id": later_cycle.cycle_id,
            "later_document_id": later_document.document_id,
            "later_batch_id": later_batch.batch_id,
            "platform_namespace": later_batch.platform,
            "source_row_index": current_row["row_index"],
        }
    )
    row_evidence_hash = _ota_digest(
        {
            "row_identity_hash": row_identity_hash,
            "later_document_sha256": later_document.sha256,
            "later_batch_fingerprint": candidate.get("later_batch_fingerprint"),
            "source_row": current_row,
            "order_proof": current_order_proof,
            "original_cycle_id": original_cycle.cycle_id,
            "original_issue_id": issue.issue_id,
            "original_batch_id": original_batch.batch_id,
            "original_diff_id": diff.diff_id,
            "proposal_id": proposal.proposal_id,
            "attempt_id": attempt.attempt_id,
            "verification_id": verification.verification_id,
        }
    )
    existing_consumptions = list(
        await db.scalars(
            select(MonthlyCloseOtaSettlementConsumption)
            .where(
                (
                    MonthlyCloseOtaSettlementConsumption.row_identity_hash
                    == row_identity_hash
                )
                | (
                    MonthlyCloseOtaSettlementConsumption.original_issue_id
                    == issue.issue_id
                )
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    exact_consumption = next(
        (
            item
            for item in existing_consumptions
            if item.row_identity_hash == row_identity_hash
            and item.row_evidence_hash == row_evidence_hash
            and item.settlement_evidence_hash == settlement_evidence_hash
            and item.original_issue_id == issue.issue_id
        ),
        None,
    )
    if existing_consumptions and exact_consumption is None:
        raise MonthlyCloseControlError(
            "ota_appeal_row_consumed",
            "该到账行已绑定其他申诉或证据已变化，请重新核对",
        )
    if issue.status == "resolved":
        if (
            exact_consumption is None
            or issue.last_seen_evidence_hash != settlement_evidence_hash
            or diff.status != ReconDiffStatus.appeal_settled
            or (diff.detail or {}).get("settlement_evidence_hash") != settlement_evidence_hash
        ):
            raise invalid
        return issue

    actor_id = actor.get("user_id") if isinstance(actor, dict) else actor.user_id
    consumption = MonthlyCloseOtaSettlementConsumption(
        consumption_id=f"MCOS-{row_identity_hash[:19]}",
        row_identity_hash=row_identity_hash,
        row_evidence_hash=row_evidence_hash,
        settlement_evidence_hash=settlement_evidence_hash,
        later_cycle_id=later_cycle.cycle_id,
        later_document_id=later_document.document_id,
        later_document_sha256=later_document.sha256,
        later_batch_id=later_batch.batch_id,
        later_batch_fingerprint=str(candidate["later_batch_fingerprint"]),
        platform_namespace=later_batch.platform,
        source_row_index=int(current_row["row_index"]),
        source_row_hash=str(current_row["row_hash"]),
        economic_facts={
            "amount": current_row["amount"],
            "checkin": current_row["checkin"],
            "checkout": current_row["checkout"],
            "row_type": current_row["row_type"],
            "order_proof": current_order_proof,
        },
        original_cycle_id=original_cycle.cycle_id,
        original_issue_id=issue.issue_id,
        original_batch_id=original_batch.batch_id,
        original_diff_id=diff.diff_id,
        proposal_id=proposal.proposal_id,
        attempt_id=attempt.attempt_id,
        verification_id=verification.verification_id,
        request_id=request_id,
        created_by=actor_id,
    )
    try:
        async with db.begin_nested():
            db.add(consumption)
            await db.flush()
    except IntegrityError as exc:
        conflict = await db.scalar(
            select(MonthlyCloseOtaSettlementConsumption).where(
                (
                    MonthlyCloseOtaSettlementConsumption.row_identity_hash
                    == row_identity_hash
                )
                | (
                    MonthlyCloseOtaSettlementConsumption.original_issue_id
                    == issue.issue_id
                )
            )
        )
        if (
            conflict is None
            or conflict.row_evidence_hash != row_evidence_hash
            or conflict.settlement_evidence_hash != settlement_evidence_hash
            or conflict.original_issue_id != issue.issue_id
        ):
            raise MonthlyCloseControlError(
                "ota_appeal_row_consumed",
                "该到账行已绑定其他申诉或证据已变化，请重新核对",
            ) from exc
        consumption = conflict
    before_status = diff.status.value
    diff.status = ReconDiffStatus.appeal_settled
    diff.detail = {
        **(diff.detail or {}),
        "settled_amount": str(candidate["settled_amount"]),
        "system_amount": str(diff.system_amount) if diff.system_amount is not None else None,
        "settled_month": str(candidate["later_billing_month"]),
        "short_paid": False,
        "settlement_candidate_id": candidate["candidate_id"],
        "settlement_evidence_hash": settlement_evidence_hash,
        "later_document_sha256": later_document.sha256,
        "later_batch_fingerprint": candidate["later_batch_fingerprint"],
        "source_row_hash": current_row["row_hash"],
    }
    await log_action_tx(
        db,
        actor_id,
        "monthly_close.ota_appeal.reconcile",
        "monthly_close_issue",
        issue.issue_id,
        before_data={
            "status": issue.status,
            "approved_evidence_hash": approved_evidence_hash,
            "proposal_id": proposal.proposal_id,
            "attempt_id": attempt.attempt_id,
            "verification_id": verification.verification_id,
            "diff_status": before_status,
        },
        after_data={
            "status": "resolved",
            "settlement_evidence_hash": settlement_evidence_hash,
            "batch_id": original_batch.batch_id,
            "diff_id": diff.diff_id,
            "later_document_id": later_document.document_id,
            "later_document_sha256": later_document.sha256,
            "later_batch_id": later_batch.batch_id,
            "later_batch_fingerprint": candidate["later_batch_fingerprint"],
            "source_row_hash": current_row["row_hash"],
            "consumption_id": consumption.consumption_id,
            "request_id": request_id,
        },
    )
    issue.status = "resolved"
    issue.last_seen_evidence_hash = settlement_evidence_hash
    issue.resolution_note = "已按平台到账证据精确核销"
    await db.flush()
    verified_issue = await db.scalar(
        select(MonthlyCloseIssueInstance)
        .where(MonthlyCloseIssueInstance.issue_id == issue.issue_id)
        .execution_options(populate_existing=True)
    )
    verified_diff = await db.scalar(
        select(ReconDiff)
        .where(ReconDiff.diff_id == diff.diff_id)
        .execution_options(populate_existing=True)
    )
    if (
        verified_issue is None
        or verified_diff is None
        or await db.get(
            MonthlyCloseOtaSettlementConsumption, consumption.consumption_id
        )
        is None
        or verified_issue.status != "resolved"
        or verified_issue.last_seen_evidence_hash != settlement_evidence_hash
        or verified_diff.status != ReconDiffStatus.appeal_settled
        or (verified_diff.detail or {}).get("settlement_evidence_hash")
        != settlement_evidence_hash
    ):
        raise MonthlyCloseControlError(
            "ota_appeal_verification_failed", "到账核销未通过确定性复核，已安全回滚"
        )
    return verified_issue


async def ota_statements_snapshot(
    db: AsyncSession, cycle: MonthlyCloseCycle
) -> dict[str, Any]:
    from app.models.monthly_close_control import MonthlyCloseIssueInstance

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
        appeal_issue_by_diff: dict[str, MonthlyCloseIssueInstance] = {}
        for diff in diffs:
            if diff.status != ReconDiffStatus.appeal_pending:
                continue
            batch = next(
                (item for item in batches if item.batch_id == diff.batch_id), None
            )
            if batch is None:
                continue
            issue = await db.scalar(
                select(MonthlyCloseIssueInstance).where(
                    MonthlyCloseIssueInstance.cycle_id == cycle.cycle_id,
                    MonthlyCloseIssueInstance.adapter_type == "ota_reconciliation",
                    MonthlyCloseIssueInstance.issue_key
                    == ota_issue_key(cycle, batch, diff, "appeal"),
                    MonthlyCloseIssueInstance.status.in_(
                        ("waiting_information", "proposed_fix", "open")
                    ),
                )
            )
            if issue is not None:
                appeal_issue_by_diff[diff.diff_id] = issue
        for diff in diffs:
            appeal_issue = appeal_issue_by_diff.get(diff.diff_id)
            issues.append(
                _issue(
                    "ota_diff_open",
                    appeal_issue.issue_id if appeal_issue is not None else diff.diff_id,
                    "OTA账单仍有未处理差异。",
                    batch_id=diff.batch_id,
                    diff_id=diff.diff_id,
                    **(
                        {"issue_id": appeal_issue.issue_id}
                        if appeal_issue is not None
                        else {"order_id": diff.order_id}
                    ),
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
            " · ".join(filter(None, [item.guest_name, item.room_name, item.check_in, item.check_out])) +
            ("：" if item.guest_name or item.room_name else "") + item.message +
            (f"；涉及 {item.amount:.2f} 元" if item.amount is not None else "") +
            (f"。处理办法：{item.next_action}" if item.next_action and item.next_action != item.message else ""),
            subject=" · ".join(filter(None, [item.guest_name, item.room_name, item.check_in, item.check_out])) or "结算核查事项",
            next_action=item.next_action,
            order_id=item.order_id,
            room_id=item.room_id,
            expense_id=item.expense_id,
            expense_date=item.expense_date,
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
                month=(issue.get("expense_date") or cycle.billing_month)[:7],
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
        {"blocking": report.blocking, "counts": report.counts,
         "fee_explanations": [item.to_dict() for item in report.explanations]},
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


# Utility reconciliation lives in its own thin adapter module so this legacy
# snapshot module does not become the home of another domain workflow.  These
# imports preserve the public adapter interface used by the control plane/API.
from app.services.monthly_close.utility_source import (  # noqa: E402,F401
    adapt_utility_source,
    build_utility_proposal,
    execute_utility_command,
    load_utility_context,
    verify_utility_command,
)
