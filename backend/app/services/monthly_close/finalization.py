"""The single transaction-local service allowed to complete a monthly close."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import func, inspect as sa_inspect, or_, select
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.audit_log import AuditLog
from app.models.monthly_close import (
    MONTHLY_CLOSE_STEP_KEYS,
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseSourceRequirement,
    MonthlyCloseStepConfirmation,
)
from app.models.monthly_close_control import (
    MonthlyCloseExecutionAttempt,
    MonthlyCloseIssueInstance,
    MonthlyCloseOutbox,
    MonthlyCloseProposal,
    MonthlyCloseRemediation,
    MonthlyCloseVerification,
)
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.user import User
from app.models.settlement import OwnerSettlement, OwnerSettlementItem
from app.services.audit import log_action_tx
from app.services.monthly_close.evidence import StepEvidence, build_step_evidences, compatible_confirmation_hashes
from app.services.monthly_close.migration import MonthlyCloseRolloutError
from app.services.monthly_close.workflow import MonthlyCloseConflict


EvidenceLoader = Callable[
    [AsyncSession, MonthlyCloseCycle], Awaitable[list[StepEvidence]]
]


def _digest(value: Any) -> str:
    encoded = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def finalization_evidence_hash(evidences: Sequence[StepEvidence]) -> str:
    return _digest(
        [
            {"evidence_hash": item.evidence_hash, "step_key": item.step_key}
            for item in evidences
        ]
    )


def _money(value: Any) -> str:
    return format(Decimal(str(value or 0)).quantize(Decimal("0.01")), ".2f")


async def _finalization_live_snapshot(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    *,
    evidence_loader: EvidenceLoader | None = None,
    exempt_attempt_id: str | None = None,
) -> dict[str, Any]:
    """Read every final-close gate into one deterministic immutable snapshot."""

    loader = evidence_loader or build_step_evidences
    evidences = await loader(db, cycle)
    evidence_hashes = {
        item.step_key: item.evidence_hash for item in evidences
    }
    confirmations = list(
        await db.scalars(
            select(MonthlyCloseStepConfirmation)
            .where(MonthlyCloseStepConfirmation.cycle_id == cycle.cycle_id)
            .order_by(MonthlyCloseStepConfirmation.step_key)
        )
    )
    confirmation_hashes = compatible_confirmation_hashes(
        evidences, {item.step_key: item for item in confirmations}
    )

    requirements = list(
        await db.scalars(
            select(MonthlyCloseSourceRequirement)
            .where(MonthlyCloseSourceRequirement.cycle_id == cycle.cycle_id)
            .order_by(MonthlyCloseSourceRequirement.source_type)
        )
    )
    documents = list(
        await db.scalars(
            select(MonthlyCloseDocument)
            .where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.is_active.is_(True),
            )
            .order_by(MonthlyCloseDocument.source_type, MonthlyCloseDocument.document_id)
        )
    )
    source_completeness = [
        {
            "source_type": item.source_type,
            "state": item.state,
            "document_ids": [
                document.document_id
                for document in documents
                if document.source_type == item.source_type
            ],
            "document_hashes": [
                document.sha256
                for document in documents
                if document.source_type == item.source_type
            ],
        }
        for item in requirements
    ]

    issues = list(
        await db.scalars(
            select(MonthlyCloseIssueInstance)
            .where(MonthlyCloseIssueInstance.cycle_id == cycle.cycle_id)
            .order_by(MonthlyCloseIssueInstance.issue_id)
        )
    )
    unresolved_issues = [
        {
            "issue_id": item.issue_id,
            "source_subject_id": item.source_subject_id,
            "status": item.status,
            "evidence_hash": item.last_seen_evidence_hash,
        }
        for item in issues
        if item.status not in {"resolved", "cancelled"}
    ]

    settlements = list(
        await db.scalars(
            select(OwnerSettlement)
            .options(selectinload(OwnerSettlement.items))
            .where(OwnerSettlement.billing_month == cycle.billing_month)
            .order_by(OwnerSettlement.owner_id, OwnerSettlement.settlement_id)
        )
    )
    year, month = (int(part) for part in cycle.billing_month.split("-"))
    month_start = date(year, month, 1)
    month_end = (
        date(year + 1, 1, 1)
        if month == 12
        else date(year, month + 1, 1)
    )
    settlement_snapshots: list[dict[str, Any]] = []
    for settlement in settlements:
        items = sorted(settlement.items, key=lambda item: item.item_id)
        room_ids = sorted(
            {item.room_id for item in items if item.room_id is not None}
        )
        order_ids = (
            sorted(
                set(
                    await db.scalars(
                        select(Order.order_id)
                        .join(OrderRoom, OrderRoom.order_id == Order.order_id)
                        .where(
                            Order.is_deleted.is_(False),
                            Order.order_status != OrderStatus.cancelled,
                            OrderRoom.room_id.in_(room_ids),
                            OrderRoom.check_out_date >= month_start,
                            OrderRoom.check_out_date < month_end,
                        )
                    )
                )
            )
            if room_ids
            else []
        )
        settlement_snapshots.append(
            {
                "settlement_id": settlement.settlement_id,
                "owner_id": settlement.owner_id,
                "status": getattr(settlement.status, "value", settlement.status),
                "item_ids": [item.item_id for item in items],
                "room_ids": room_ids,
                "order_ids": order_ids,
                "total_net_revenue": _money(settlement.total_net_revenue),
                "owner_amount": _money(settlement.owner_amount),
                "deducted_expenses": _money(settlement.deducted_expenses),
                "actual_owner_amount": _money(settlement.actual_owner_amount),
                "items": [
                    {
                        "item_id": item.item_id,
                        "room_id": item.room_id,
                        "label": item.label,
                        "net_revenue": _money(item.net_revenue),
                        "owner_expenses": _money(item.owner_expenses),
                        "owner_net_amount": _money(item.owner_net_amount),
                        "share_ratio_snapshot": format(
                            Decimal(str(item.share_ratio_snapshot or 0)).quantize(
                                Decimal("0.001")
                            ),
                            ".3f",
                        ),
                    }
                    for item in items
                ],
            }
        )

    from app.services.settlement_preflight import run_settlement_preflight

    preflight = await run_settlement_preflight(db, year, month)
    preflight_snapshot = preflight.gate_dict()
    open_remediation_ids = list(
        await db.scalars(
            select(MonthlyCloseRemediation.remediation_id)
            .where(
                MonthlyCloseRemediation.cycle_id == cycle.cycle_id,
                MonthlyCloseRemediation.status != "resolved",
            )
            .order_by(MonthlyCloseRemediation.remediation_id)
        )
    )
    unsafe_attempt_ids = list(
        await db.scalars(
            select(MonthlyCloseExecutionAttempt.attempt_id)
            .where(
                MonthlyCloseExecutionAttempt.cycle_id == cycle.cycle_id,
                MonthlyCloseExecutionAttempt.status.not_in(
                    ("failed_safe", "failed_confirmed", "verified")
                ),
                MonthlyCloseExecutionAttempt.attempt_id != exempt_attempt_id
                if exempt_attempt_id is not None
                else True,
            )
            .order_by(MonthlyCloseExecutionAttempt.attempt_id)
        )
    )
    return {
        "control_version": cycle.control_version,
        "evidence_hashes": evidence_hashes,
        "blocking_steps": [
            item.step_key for item in evidences if item.blocking_count
        ],
        "step_confirmation_hashes": confirmation_hashes,
        "source_completeness": source_completeness,
        "unresolved_issues": unresolved_issues,
        "settlements": settlement_snapshots,
        "owner_confirmations": [
            {
                "owner_id": item["owner_id"],
                "settlement_id": item["settlement_id"],
                "status": item["status"],
            }
            for item in settlement_snapshots
        ],
        "preflight_hash": _digest(preflight_snapshot),
        "preflight": preflight_snapshot,
        "open_remediation_ids": open_remediation_ids,
        "unsafe_attempt_ids": unsafe_attempt_ids,
    }


def _assert_finalization_snapshot_ready(snapshot: dict[str, Any]) -> None:
    evidence_hashes = snapshot["evidence_hashes"]
    if list(evidence_hashes) != list(MONTHLY_CLOSE_STEP_KEYS):
        raise MonthlyCloseConflict(
            "finalization_evidence_incomplete", "九项月结证据不完整，不能完成月结"
        )
    if snapshot["step_confirmation_hashes"] != evidence_hashes:
        raise MonthlyCloseConflict(
            "finalization_steps_incomplete", "九项月结确认与当前证据不一致，不能完成月结"
        )
    if snapshot["blocking_steps"]:
        raise MonthlyCloseConflict(
            "finalization_blocked", "仍有月结阻断项，不能完成月结"
        )
    if any(item["state"] == "pending" for item in snapshot["source_completeness"]):
        raise MonthlyCloseConflict(
            "finalization_sources_incomplete", "月结资料仍不完整，不能进入最终复核"
        )
    if snapshot["unresolved_issues"]:
        raise MonthlyCloseConflict(
            "finalization_issues_open", "仍有未解决异常，不能完成月结"
        )
    if snapshot["open_remediation_ids"] or snapshot["unsafe_attempt_ids"]:
        raise MonthlyCloseConflict(
            "finalization_remediation_open", "仍有执行结果待确认或补救，不能完成月结"
        )
    if snapshot["preflight"]["blocking"]:
        raise MonthlyCloseConflict(
            "finalization_preflight_failed", "月结体检仍有阻断项，不能完成月结"
        )
    if any(
        item["status"] not in {"confirmed", "paid"}
        for item in snapshot["owner_confirmations"]
    ):
        raise MonthlyCloseConflict(
            "finalization_owner_confirmation_missing", "仍有业主结算未确认"
        )


async def list_issue_history(
    db: AsyncSession, issue_id: str
) -> list[dict[str, Any]]:
    rows = list(
        await db.scalars(
            select(MonthlyCloseOutbox)
            .where(
                MonthlyCloseOutbox.topic == "monthly_close.issue_history",
            )
            .order_by(MonthlyCloseOutbox.created_at, MonthlyCloseOutbox.outbox_id)
        )
    )
    return [
        dict(row.payload)
        for row in rows
        if isinstance(row.payload, dict) and row.payload.get("issue_id") == issue_id
    ]


async def invalidate_monthly_close_subject(
    db: AsyncSession,
    *,
    cycle_id: str,
    subject_type: str,
    subject_id: str,
    event_id: str,
    cause: str,
    actor_id: str | None,
    evidence_hash: str,
) -> None:
    """Invalidate one exact business subject and true downstream artifacts.

    The durable outbox identity makes repeated domain notifications idempotent.
    The completed snapshot is retained as historical evidence.
    """

    dedupe_key = f"domain-change:{cycle_id}:{event_id}"
    if await db.scalar(
        select(MonthlyCloseOutbox.outbox_id).where(
            MonthlyCloseOutbox.dedupe_key == dedupe_key
        )
    ) is not None:
        return
    cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle_id)
        .with_for_update()
    )
    if cycle is None:
        raise MonthlyCloseConflict("cycle_not_found", "月结周期不存在")

    subject_candidates = {
        subject_id,
        f"{subject_type}:{subject_id}",
        f"order:{subject_id}" if subject_type == "order_refund" else "",
    }
    subject_candidates.discard("")
    issues = list(
        await db.scalars(
            select(MonthlyCloseIssueInstance)
            .where(
                MonthlyCloseIssueInstance.cycle_id == cycle_id,
                MonthlyCloseIssueInstance.source_subject_id.in_(subject_candidates),
            )
            .with_for_update()
        )
    )
    refund_issue_identity = None
    if subject_type == "order_refund":
        refund_issue_identity = {
            "adapter_type": "order_refund_domain_change",
            "source_subject_id": f"order:{subject_id}",
            "issue_key": "post-close-refund-change",
        }
        existing_refund_issue = next(
            (
                issue
                for issue in issues
                if issue.adapter_type == refund_issue_identity["adapter_type"]
                and issue.source_subject_id == refund_issue_identity["source_subject_id"]
                and issue.issue_key == refund_issue_identity["issue_key"]
            ),
            None,
        )
        if existing_refund_issue is None:
            db.add(
                MonthlyCloseIssueInstance(
                    issue_id=f"MCI-{uuid4().hex[:20].upper()}",
                    cycle_id=cycle_id,
                    adapter_type=refund_issue_identity["adapter_type"],
                    source_subject_id=refund_issue_identity["source_subject_id"],
                    issue_key=refund_issue_identity["issue_key"],
                    status="open",
                    manual_note=(
                        f"订单 {subject_id} 在月结后发生退款变更（{cause}，事件 {event_id}），"
                        "需重新核对订单、退款与业主结算。"
                    ),
                    last_seen_evidence_hash=evidence_hash,
                )
            )
    for issue in issues:
        previous_hash = issue.last_seen_evidence_hash
        previous_status = issue.status
        is_refund_domain_issue = bool(
            refund_issue_identity
            and issue.adapter_type == refund_issue_identity["adapter_type"]
            and issue.source_subject_id == refund_issue_identity["source_subject_id"]
            and issue.issue_key == refund_issue_identity["issue_key"]
        )
        if issue.status in {"resolved", "cancelled"} or is_refund_domain_issue:
            issue.status = "reopened"
            issue.reopened_at = datetime.now(timezone.utc)
        history_key = f"issue-domain:{event_id}:{issue.issue_id}"
        db.add(
            MonthlyCloseOutbox(
                outbox_id=f"MCO-{uuid4().hex[:20].upper()}",
                cycle_id=cycle_id,
                topic="monthly_close.issue_history",
                payload={
                    "issue_id": issue.issue_id,
                    "kind": "domain_change_reopen",
                    "cause": cause,
                    "event_id": event_id,
                    "subject_type": subject_type,
                    "subject_id": subject_id,
                    "previous_status": previous_status,
                    "previous_evidence_hash": previous_hash,
                    "current_evidence_hash": evidence_hash,
                    "resolution_proposal_id": issue.resolution_proposal_id,
                    "verification_id": issue.verification_id,
                },
                dedupe_key=history_key,
            )
        )
        issue.last_seen_evidence_hash = evidence_hash
        issue.resolution_proposal_id = None
        issue.verification_id = None
        if is_refund_domain_issue:
            issue.manual_note = (
                f"订单 {subject_id} 在月结后发生退款变更（{cause}，事件 {event_id}），"
                "需重新核对订单、退款与业主结算。"
            )

    proposals = list(
        await db.scalars(
            select(MonthlyCloseProposal)
            .where(
                MonthlyCloseProposal.cycle_id == cycle_id,
                MonthlyCloseProposal.status.in_(("pending_approval", "approved")),
            )
            .with_for_update()
        )
    )
    downstream_types = {"generate_owner_settlements", "finalize_monthly_close"}
    for proposal in proposals:
        exact_dependency = any(
            isinstance(ref, dict)
            and ref.get("kind") == subject_type
            and ref.get("subject_id") == subject_id
            for ref in (proposal.evidence_refs or [])
        )
        if exact_dependency or proposal.proposal_type in downstream_types:
            proposal.status = "stale"

    now = datetime.now(timezone.utc)
    if cycle.status == "completed":
        cycle.status = "reopened"
        cycle.reopened_by = actor_id
        cycle.reopened_at = now
        cycle.reopen_reason = f"{cause}:{subject_type}:{subject_id}"
        cycle.monitor_effective_status = "reopened"
        cycle.monitor_current_step = "preflight"
        cycle.monitor_progress = 6

    confirmations = list(
        await db.scalars(
            select(MonthlyCloseStepConfirmation)
            .where(
                MonthlyCloseStepConfirmation.cycle_id == cycle_id,
                MonthlyCloseStepConfirmation.step_key.in_(
                    ("preflight", "settlement_review", "owner_confirmation")
                ),
            )
            .with_for_update()
        )
    )
    for confirmation in confirmations:
        previous = {
            "evidence_hash": confirmation.evidence_hash,
            "evidence": confirmation.evidence,
            "note": confirmation.note,
            "confirmed_by": confirmation.confirmed_by,
            "confirmed_at": confirmation.confirmed_at.isoformat(),
        }
        confirmation.history = [*(confirmation.history or []), previous]
        confirmation.evidence_hash = _digest(
            {
                "event_id": event_id,
                "previous_evidence_hash": previous["evidence_hash"],
                "step_key": confirmation.step_key,
            }
        )
        confirmation.evidence = {
            "_state": "domain_change_invalidated",
            "cause": cause,
            "event_id": event_id,
            "subject_type": subject_type,
            "subject_id": subject_id,
            "previous_evidence_hash": previous["evidence_hash"],
        }

    db.add(
        MonthlyCloseOutbox(
            outbox_id=f"MCO-{uuid4().hex[:20].upper()}",
            cycle_id=cycle_id,
            topic="monthly_close.domain_changed",
            payload={
                "actor_id": actor_id,
                "cause": cause,
                "event_id": event_id,
                "evidence_hash": evidence_hash,
                "subject_type": subject_type,
                "subject_id": subject_id,
            },
            dedupe_key=dedupe_key,
        )
    )
    await db.flush()


async def invalidate_monthly_close_order_refund(
    db: AsyncSession,
    *,
    billing_month: str,
    order_id: str,
    refund_id: str,
    operation: str,
    actor_id: str | None,
    amount: Decimal,
    reason: str,
) -> None:
    """Bind a committed refund mutation to its exact assistant-owned cycle.

    The caller owns the surrounding transaction: refund write, audit, outbox,
    and monthly-close invalidation therefore commit or roll back together.
    Legacy cycles deliberately retain their existing write path.
    """

    cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(
            MonthlyCloseCycle.billing_month == billing_month,
            MonthlyCloseCycle.write_control_owner == "assistant",
        )
        .with_for_update()
    )
    if cycle is None:
        return
    evidence_hash = _digest(
        {
            "amount": _money(amount),
            "billing_month": billing_month,
            "operation": operation,
            "order_id": order_id,
            "reason": reason,
            "refund_id": refund_id,
        }
    )
    await invalidate_monthly_close_subject(
        db,
        cycle_id=cycle.cycle_id,
        subject_type="order_refund",
        subject_id=order_id,
        event_id=f"refund:{operation}:{refund_id}",
        cause=f"refund_{operation}_after_close",
        actor_id=actor_id,
        evidence_hash=evidence_hash,
    )


async def _current_admin(db: AsyncSession, actor: Any) -> User:
    if isinstance(actor, dict):
        actor_id = actor.get("user_id")
    else:
        try:
            identity = sa_inspect(actor).identity
        except NoInspectionAvailable:
            identity = None
        actor_id = identity[0] if identity else getattr(actor, "user_id", None)
    user = (
        await db.scalar(
            select(User)
            .where(User.user_id == actor_id)
            .execution_options(populate_existing=True)
        )
        if isinstance(actor_id, str)
        else None
    )
    if (
        user is None
        or not user.is_active
        or getattr(user.role, "value", user.role) != "admin"
    ):
        raise MonthlyCloseConflict(
            "finalization_forbidden", "当前账号无权完成月结"
        )
    return user


async def build_settlement_proposal(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    actor: Any,
    *,
    request_id: str,
    overwrite: bool,
) -> MonthlyCloseProposal:
    from app.api.v1.settlements import build_settlement_plan_snapshot
    from app.services.monthly_close.control import create_proposal

    year, month = (int(part) for part in cycle.billing_month.split("-"))
    plan = await build_settlement_plan_snapshot(
        db, year, month, overwrite=overwrite
    )
    evidence_refs = [
        {
            "kind": "settlement_plan",
            "subject_id": item["settlement_id"],
            "evidence_hash": _digest(item),
        }
        for item in plan["settlements"]
    ]
    command = {
        "command_type": "generate_owner_settlements",
        "subject_id": cycle.cycle_id,
        "before": {
            "billing_month": cycle.billing_month,
            "locked_settlements": plan["locked_settlements"],
            "settlement_ids": [],
        },
        "after": plan,
        "amount_impact": plan["total_amount"],
        "evidence_refs": evidence_refs,
        "business_idempotency_key": (
            f"settlements:{cycle.cycle_id}:{plan['plan_hash'][:32]}"
        ),
    }
    return await create_proposal(
        db,
        cycle,
        actor,
        [command],
        evidence_hash=plan["plan_hash"],
        proposal_type="generate_owner_settlements",
        request_id=request_id,
        evidence_refs=evidence_refs,
        impact_snapshot={
            "billing_month": cycle.billing_month,
            "settlement_count": len(plan["settlements"]),
            "settlement_ids": plan["settlement_ids"],
            "owner_ids": plan["owner_ids"],
            "total_amount": plan["total_amount"],
            "preflight_hash": plan["preflight_hash"],
            "overwrite": overwrite,
        },
    )


async def load_settlement_context(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    commands: Sequence[dict[str, Any]],
):
    from app.api.v1.settlements import build_settlement_plan_snapshot
    from app.services.monthly_close.control import ProposalFreshnessContext

    if len(commands) != 1:
        raise MonthlyCloseConflict(
            "settlement_command_invalid", "业主结算方案必须是一个完整原子命令"
        )
    approved = commands[0].get("after") or {}
    year, month = (int(part) for part in cycle.billing_month.split("-"))
    current = await build_settlement_plan_snapshot(
        db, year, month, overwrite=bool(approved.get("overwrite"))
    )
    return ProposalFreshnessContext(
        evidence_hash=current["plan_hash"],
        subject_versions={
            cycle.cycle_id: cycle.control_version,
            "settlement_plan": current["plan_hash"],
        },
        active_input_set_hash=current["plan_hash"],
        mapping_versions={},
        ruleset_version=current["ruleset_version"],
        calculation_version=current["calculation_version"],
        configuration_snapshot_hash=_digest(
            {
                "billing_month": cycle.billing_month,
                "overwrite": current["overwrite"],
                "ruleset_version": current["ruleset_version"],
            }
        ),
        control_version=cycle.control_version,
    )


async def execute_settlement_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: MonthlyCloseProposal,
    command: dict[str, Any],
    actor: User,
    request_id: str,
) -> dict[str, Any]:
    from app.api.v1.settlements import _generate_settlements_core

    year, month = (int(part) for part in cycle.billing_month.split("-"))
    plan = command["after"]
    expected_version = proposal.subject_versions.get(
        cycle.cycle_id, proposal.subject_versions.get("cycle")
    )
    if not isinstance(expected_version, int):
        raise MonthlyCloseConflict(
            "write_control_request_invalid",
            "结算方案缺少不可变的月结控制版本",
        )
    try:
        result = await _generate_settlements_core(
            db,
            year,
            month,
            actor.user_id,
            overwrite=bool(plan["overwrite"]),
            expected_cycle_id=cycle.cycle_id,
            expected_write_owner="assistant",
            expected_control_version=expected_version,
            commit=False,
            approved_plan=plan,
        )
    except MonthlyCloseRolloutError as exc:
        raise MonthlyCloseConflict(exc.code, exc.message, detail=exc.detail) from exc
    await log_action_tx(
        db,
        actor.user_id,
        "monthly_close.settlements.execute",
        "monthly_close",
        cycle.cycle_id,
        before_data=command["before"],
        after_data={
            "approved_after": command["after"],
            "business_idempotency_key": command["business_idempotency_key"],
            "proposal_id": proposal.proposal_id,
            "request_id": request_id,
            "result": result,
        },
    )
    await db.flush()
    audit_id = await db.scalar(
        select(func.max(AuditLog.log_id)).where(
            AuditLog.action == "monthly_close.settlements.execute",
            AuditLog.resource_id == cycle.cycle_id,
            AuditLog.operator_id == actor.user_id,
        )
    )
    if audit_id is None:
        raise MonthlyCloseConflict(
            "settlement_audit_missing", "结算写入未生成事务审计"
        )
    return {
        "result": {
            "billing_month": cycle.billing_month,
            "plan_hash": plan["plan_hash"],
            "settlement_ids": plan["settlement_ids"],
        },
        "audit_refs": [str(audit_id)],
    }


async def _settlement_plan_is_applied(
    db: AsyncSession, plan: dict[str, Any]
) -> bool:
    from app.api.v1.settlements import (
        SETTLEMENT_CALCULATION_VERSION,
        SETTLEMENT_RULESET_VERSION,
        _build_settlement_financial_snapshot,
        _canonical_settlement_item_facts,
        _existing_settlement_snapshot,
        _sponsorship_binding_snapshot,
    )
    from app.models.company_sponsored_stay import CompanySponsorshipStatus
    from app.models.expense import Expense
    from app.models.room import Room
    from app.services.owner_settlement import load_room_sponsorship_income

    if (
        plan.get("ruleset_version") != SETTLEMENT_RULESET_VERSION
        or plan.get("calculation_version") != SETTLEMENT_CALCULATION_VERSION
    ):
        return False
    year, month = (int(part) for part in plan["billing_month"].split("-"))
    period_start = date(year, month, 1)
    period_end = (
        date(year + 1, 1, 1)
        if month == 12
        else date(year, month + 1, 1)
    )
    all_month_rows = list(
        await db.scalars(
            select(OwnerSettlement)
            .where(OwnerSettlement.billing_month == plan["billing_month"])
            .order_by(OwnerSettlement.settlement_id)
        )
    )
    expected_month_ids = set(plan["settlement_ids"]) | {
        item["settlement_id"] for item in plan.get("preserved_settlements", [])
    }
    if {row.settlement_id for row in all_month_rows} != expected_month_ids:
        return False
    actual_by_id = {row.settlement_id: row for row in all_month_rows}
    for preserved in plan.get("preserved_settlements", []):
        row = actual_by_id[preserved["settlement_id"]]
        current_preserved = await _existing_settlement_snapshot(
            db, row, year, month
        )
        if current_preserved != preserved:
            return False
    expected_locked = [
        item
        for item in plan.get("preserved_settlements", [])
        if item["status"] != "pending"
    ]
    if plan.get("locked_settlements", []) != expected_locked:
        return False

    rows = list(
        await db.scalars(
            select(OwnerSettlement)
            .where(OwnerSettlement.settlement_id.in_(plan["settlement_ids"]))
            .order_by(OwnerSettlement.settlement_id)
        )
    )
    if {row.settlement_id for row in rows} != set(plan["settlement_ids"]):
        return False
    expected_by_id = {
        item["settlement_id"]: item for item in plan["settlements"]
    }
    for row in rows:
        expected = expected_by_id[row.settlement_id]
        if (
            row.owner_id != expected["owner_id"]
            or row.billing_month != plan["billing_month"]
            or getattr(row.status, "value", row.status) != expected["status"]
            or format(Decimal(row.total_net_revenue), ".2f")
            != expected["total_net_revenue"]
            or format(Decimal(row.owner_amount), ".2f") != expected["owner_amount"]
            or format(Decimal(row.deducted_expenses), ".2f")
            != expected["deducted_expenses"]
            or format(Decimal(row.actual_owner_amount), ".2f")
            != expected["actual_owner_amount"]
        ):
            return False
        items = list(
            await db.scalars(
                select(OwnerSettlementItem)
                .where(OwnerSettlementItem.settlement_id == row.settlement_id)
                .order_by(OwnerSettlementItem.item_id)
            )
        )
        expected_items = {
            item_id: item
            for item_id, item in zip(expected["item_ids"], expected["items"])
        }
        if {item.item_id for item in items} != set(expected_items):
            return False
        for item in items:
            expected_item = expected_items[item.item_id]
            actual_item = {
                "room_id": item.room_id,
                "label": item.label,
                "order_count": int(item.order_count),
                "revenue": _money(item.revenue),
                "commission": _money(item.commission),
                "net_revenue": _money(item.net_revenue),
                "externally_settled_income": _money(
                    item.externally_settled_income
                ),
                "owner_expenses": _money(item.owner_expenses),
                "share_ratio_snapshot": format(
                    Decimal(str(item.share_ratio_snapshot or 0)).quantize(
                        Decimal("0.001")
                    ),
                    ".3f",
                ),
                "owner_net_amount": _money(item.owner_net_amount),
                "cost_share_breakdown": item.cost_share_breakdown or [],
            }
            if actual_item != expected_item:
                return False

        rooms = list(
            await db.scalars(
                select(Room)
                .where(
                    Room.owner_id == expected["owner_id"],
                    Room.room_id.in_(expected["room_ids"]),
                )
                .order_by(Room.room_id)
            )
        )
        if [room.room_id for room in rooms] != expected["room_ids"]:
            return False
        service_fee_refs = {
            expense.expense_id: (
                f"{expense.order_id}:{expense.room_id}:{expense.category.value}"
            )
            for expense in await db.scalars(
                select(Expense).where(
                    Expense.owner_id == expected["owner_id"],
                    Expense.is_service_fee.is_(True),
                    Expense.is_deleted.is_(False),
                    Expense.expense_date >= period_start,
                    Expense.expense_date < period_end,
                )
            )
        }
        current = await _build_settlement_financial_snapshot(
            db, expected["owner_id"], year, month, rooms=rooms
        )
        if (
            _canonical_settlement_item_facts(current, service_fee_refs)
            != expected["items"]
            or _money(current.total_revenue) != expected["total_revenue"]
            or _money(current.total_net_revenue) != expected["total_net_revenue"]
            or _money(current.owner_amount) != expected["owner_amount"]
            or _money(current.deducted_expenses) != expected["deducted_expenses"]
            or _money(current.actual_owner_amount)
            != expected["actual_owner_amount"]
        ):
            return False

        room_item_ids = {
            item["room_id"]: item_id
            for item_id, item in zip(expected["item_ids"], expected["items"])
            if item["room_id"] is not None
        }
        sponsorship_by_room = await load_room_sponsorship_income(
            db, expected["room_ids"], year, month, for_update=True
        )
        actual_bindings = []
        for room_id in expected["room_ids"]:
            for income in sponsorship_by_room.get(room_id, []):
                if (
                    income.root.status != CompanySponsorshipStatus.settled
                    or income.root.settlement_batch_id != row.settlement_id
                    or income.root.settlement_item_id != room_item_ids[room_id]
                ):
                    return False
                actual_bindings.append(
                    _sponsorship_binding_snapshot(
                        income,
                        room_id=room_id,
                        settlement_item_id=room_item_ids[room_id],
                    )
                )
        actual_bindings.sort(
            key=lambda item: (
                item["sponsorship_id"], item["settlement_item_id"]
            )
        )
        expected_bindings = [
            {**item, "status": CompanySponsorshipStatus.settled.value}
            for item in expected["sponsorship_bindings"]
        ]
        if actual_bindings != expected_bindings:
            return False
    return True


async def verify_settlement_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: MonthlyCloseProposal,
    command: dict[str, Any],
    attempt: MonthlyCloseExecutionAttempt,
) -> dict[str, Any]:
    from app.api.v1.settlements import _settlement_digest
    from app.services.settlement_preflight import run_settlement_preflight

    plan = command["after"]
    year, month = (int(part) for part in cycle.billing_month.split("-"))
    preflight = await run_settlement_preflight(db, year, month)
    preflight_hash = _settlement_digest(preflight.gate_dict())
    applied = await _settlement_plan_is_applied(db, plan)
    audit = await db.scalar(
        select(AuditLog)
        .where(
            AuditLog.action == "monthly_close.settlements.execute",
            AuditLog.resource_id == cycle.cycle_id,
            AuditLog.operator_id == attempt.actor_id,
        )
        .order_by(AuditLog.log_id.desc())
    )
    audit_present = bool(
        audit is not None
        and audit.before_data == command["before"]
        and isinstance(audit.after_data, dict)
        and audit.after_data.get("approved_after") == plan
        and audit.after_data.get("business_idempotency_key")
        == command["business_idempotency_key"]
        and audit.after_data.get("proposal_id") == proposal.proposal_id
        and audit.after_data.get("request_id") == attempt.request_id
    )
    target_matches = applied and preflight_hash == plan["preflight_hash"]
    outcome = "applied" if target_matches and audit_present else "inconclusive"
    expected_version = proposal.subject_versions.get(
        cycle.cycle_id, proposal.subject_versions.get("cycle")
    )
    return {
        "outcome": outcome,
        "business_idempotency_key": command["business_idempotency_key"],
        "subject": {
            "subject_id": cycle.cycle_id,
            "expected_version": expected_version,
            "actual_version": cycle.control_version,
        },
        "target": {
            "expected_before": command["before"],
            "expected_after": plan,
            "actual": plan if target_matches else {"plan_hash": None},
        },
        "amount": {
            "expected": command["amount_impact"],
            "actual": command["amount_impact"] if target_matches else "0.00",
        },
        "audit": {
            "present": audit_present,
            "request_id": attempt.request_id,
            "business_idempotency_key": command["business_idempotency_key"],
            "expected_before": command["before"],
            "expected_after": plan,
        },
        "idempotency": {
            "present": audit_present,
            "request_id": attempt.request_id,
            "business_idempotency_key": command["business_idempotency_key"],
        },
        "evidence": {
            "approved_evidence_hash": proposal.evidence_hash,
            "verification_evidence_hash": _digest(
                {
                    "applied": applied,
                    "attempt_id": attempt.attempt_id,
                    "audit_present": audit_present,
                    "preflight_hash": preflight_hash,
                }
            ),
        },
        "issues": [],
    }


async def build_finalization_proposal(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    actor: Any,
    *,
    request_id: str,
    evidence_loader: EvidenceLoader | None = None,
) -> MonthlyCloseProposal:
    """Prepare the only command allowed to transition a cycle to completed."""

    from app.services.monthly_close.control import (
        CommandHandlerRegistry,
        create_proposal,
    )

    snapshot = await _finalization_live_snapshot(
        db, cycle, evidence_loader=evidence_loader
    )
    _assert_finalization_snapshot_ready(snapshot)
    snapshot_hash = _digest(snapshot)
    evidence_refs = [
        {
            "kind": "monthly_close_step",
            "step_key": step_key,
            "evidence_hash": evidence_hash,
        }
        for step_key, evidence_hash in snapshot["evidence_hashes"].items()
    ]
    evidence_refs.extend(
        {
            "kind": "monthly_close_source",
            "subject_id": item["source_type"],
            "evidence_hash": _digest(item),
        }
        for item in snapshot["source_completeness"]
    )
    evidence_refs.extend(
        {
            "kind": "owner_settlement",
            "subject_id": item["settlement_id"],
            "evidence_hash": _digest(item),
        }
        for item in snapshot["settlements"]
    )
    command = {
        "command_type": "finalize_monthly_close",
        "subject_id": cycle.cycle_id,
        "before": {"status": cycle.status},
        "after": {"status": "completed", **snapshot},
        "amount_impact": "0.00",
        "evidence_refs": evidence_refs,
        "business_idempotency_key": (
            f"finalize:{cycle.cycle_id}:{snapshot_hash[:32]}"
        ),
    }

    handler_registry = None
    if evidence_loader is not None:
        handler_registry = CommandHandlerRegistry()

        async def load_custom_context(
            context_db: AsyncSession,
            context_cycle: MonthlyCloseCycle,
            commands: Sequence[dict[str, Any]],
        ):
            return await load_finalize_context(
                context_db,
                context_cycle,
                commands,
                evidence_loader=evidence_loader,
            )

        handler_registry.register(
            "finalize_monthly_close",
            execute=execute_finalize_command,
            verify=verify_finalize_command,
            load_context=load_custom_context,
        )
    return await create_proposal(
        db,
        cycle,
        actor,
        [command],
        evidence_hash=snapshot_hash,
        proposal_type="finalize_monthly_close",
        request_id=request_id,
        evidence_refs=evidence_refs,
        impact_snapshot={
            "billing_month": cycle.billing_month,
            "source_count": len(snapshot["source_completeness"]),
            "settlement_count": len(snapshot["settlements"]),
            "settlement_ids": [
                item["settlement_id"] for item in snapshot["settlements"]
            ],
            "preflight_hash": snapshot["preflight_hash"],
            "unresolved_issue_count": len(snapshot["unresolved_issues"]),
        },
        handler_registry=handler_registry,
    )


async def finalize_monthly_close(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    actor: Any,
    *,
    request_id: str,
    business_idempotency_key: str | None = None,
    executing_attempt_id: str | None = None,
    executing_proposal_id: str | None = None,
    expected_control_version: int | None = None,
    expected_evidence_hashes: dict[str, str] | None = None,
    expected_finalization_snapshot: dict[str, Any] | None = None,
    evidence_loader: EvidenceLoader | None = None,
) -> MonthlyCloseCycle:
    """Reload every live gate and stage completion without committing.

    The caller owns commit/rollback.  This is essential: the ninth legacy
    confirmation and proposal execution must persist their confirmation,
    business audit, attempt, and completed cycle as one atomic transaction.
    """

    current_actor = await _current_admin(db, actor)
    if (
        executing_attempt_id is None
        or executing_proposal_id is None
        or business_idempotency_key is None
    ):
        raise MonthlyCloseConflict(
            "finalization_attempt_lineage_required",
            "完成月结必须来自已批准方案的执行记录",
        )
    locked = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked is None:
        raise MonthlyCloseConflict("cycle_not_found", "月结周期不存在")
    if locked.status == "completed":
        snapshot = locked.final_snapshot if isinstance(locked.final_snapshot, dict) else {}
        if snapshot.get("request_id") == request_id:
            return locked
        raise MonthlyCloseConflict("cycle_completed", "本月月结已经完成")
    if (
        expected_control_version is not None
        and locked.control_version != expected_control_version
    ):
        raise MonthlyCloseConflict(
            "finalization_control_changed",
            "月结写入控制版本已经变化，请重新生成关账方案",
        )

    exempt_attempt_id: str | None = None
    if executing_attempt_id is not None or executing_proposal_id is not None:
        lineage = (
            await db.execute(
                select(MonthlyCloseExecutionAttempt, MonthlyCloseProposal)
                .join(
                    MonthlyCloseProposal,
                    MonthlyCloseProposal.proposal_id
                    == MonthlyCloseExecutionAttempt.proposal_id,
                )
                .where(
                    MonthlyCloseExecutionAttempt.attempt_id
                    == executing_attempt_id,
                    MonthlyCloseExecutionAttempt.cycle_id == locked.cycle_id,
                    MonthlyCloseExecutionAttempt.proposal_id
                    == executing_proposal_id,
                    MonthlyCloseExecutionAttempt.request_id == request_id,
                    MonthlyCloseExecutionAttempt.status == "executing",
                    MonthlyCloseProposal.cycle_id == locked.cycle_id,
                    MonthlyCloseProposal.proposal_type
                    == "finalize_monthly_close",
                    MonthlyCloseExecutionAttempt.proposal_binding_hash
                    == MonthlyCloseProposal.proposal_binding_hash,
                )
                .with_for_update()
            )
        ).first()
        if lineage is None:
            raise MonthlyCloseConflict(
                "finalization_attempt_lineage_invalid",
                "关账执行记录归属无效，不能跳过安全检查",
            )
        _attempt, proposal = lineage
        # Imported lazily because the control registry imports this module to
        # register the one explicit finalization handler.
        from app.services.monthly_close.control import proposal_binding_is_valid

        commands = proposal.canonical_payload.get("commands", [])
        if (
            not proposal_binding_is_valid(proposal)
            or len(commands) != 1
            or commands[0].get("command_type") != "finalize_monthly_close"
            or commands[0].get("subject_id") != locked.cycle_id
            or commands[0].get("business_idempotency_key")
            != business_idempotency_key
        ):
            raise MonthlyCloseConflict(
                "finalization_attempt_lineage_invalid",
                "关账执行记录归属无效，不能跳过安全检查",
            )
        exempt_attempt_id = executing_attempt_id

    live_finalization_snapshot = await _finalization_live_snapshot(
        db,
        locked,
        evidence_loader=evidence_loader,
        exempt_attempt_id=exempt_attempt_id,
    )
    _assert_finalization_snapshot_ready(live_finalization_snapshot)
    if (
        expected_finalization_snapshot is None
        or expected_finalization_snapshot != live_finalization_snapshot
    ):
        raise MonthlyCloseConflict(
            "finalization_evidence_changed",
            "关账证据已经变化，请刷新后重新确认",
            detail={
                "current_finalization_snapshot_hash": _digest(
                    live_finalization_snapshot
                )
            },
        )

    loader = evidence_loader or build_step_evidences
    evidences = await loader(db, locked)
    if (
        len(evidences) != len(MONTHLY_CLOSE_STEP_KEYS)
        or [item.step_key for item in evidences] != list(MONTHLY_CLOSE_STEP_KEYS)
    ):
        raise MonthlyCloseConflict(
            "finalization_evidence_incomplete", "九项月结证据不完整，不能完成月结"
        )
    live_hashes = {item.step_key: item.evidence_hash for item in evidences}
    if expected_evidence_hashes is not None and live_hashes != expected_evidence_hashes:
        raise MonthlyCloseConflict(
            "finalization_evidence_changed",
            "关账证据已经变化，请刷新后重新确认",
            detail={"current_evidence_hashes": live_hashes},
        )
    blocked = [item for item in evidences if item.blocking_count]
    if blocked:
        raise MonthlyCloseConflict(
            "finalization_blocked",
            "仍有月结阻断项，不能完成月结",
            detail={
                "blocking_steps": [item.step_key for item in blocked],
                "blocking_count": sum(item.blocking_count for item in blocked),
            },
        )

    confirmations = list(
        await db.scalars(
            select(MonthlyCloseStepConfirmation)
            .where(MonthlyCloseStepConfirmation.cycle_id == locked.cycle_id)
            .with_for_update()
        )
    )
    confirmation_hashes = compatible_confirmation_hashes(
        evidences, {item.step_key: item for item in confirmations}
    )
    if confirmation_hashes != live_hashes:
        raise MonthlyCloseConflict(
            "finalization_steps_incomplete",
            "九项月结确认与当前证据不一致，不能完成月结",
        )

    open_remediation_count = int(
        await db.scalar(
            select(func.count())
            .select_from(MonthlyCloseRemediation)
            .where(
                MonthlyCloseRemediation.cycle_id == locked.cycle_id,
                MonthlyCloseRemediation.status != "resolved",
            )
        )
        or 0
    )
    unsafe_attempt_count = int(
        await db.scalar(
            select(func.count())
            .select_from(MonthlyCloseExecutionAttempt)
            .where(
                MonthlyCloseExecutionAttempt.cycle_id == locked.cycle_id,
                MonthlyCloseExecutionAttempt.status.not_in(
                    ("failed_safe", "failed_confirmed", "verified")
                ),
                MonthlyCloseExecutionAttempt.attempt_id != exempt_attempt_id
                if exempt_attempt_id is not None
                else True,
            )
        )
        or 0
    )
    if open_remediation_count or unsafe_attempt_count:
        raise MonthlyCloseConflict(
            "finalization_remediation_open",
            "仍有执行结果待确认或补救，不能完成月结",
        )

    prior_status = locked.status
    now = datetime.now(timezone.utc)
    locked.status = "completed"
    locked.completed_by = current_actor.user_id
    locked.completed_at = now
    locked.monitor_effective_status = "completed"
    locked.monitor_current_step = None
    locked.monitor_progress = len(MONTHLY_CLOSE_STEP_KEYS)
    locked.monitor_blocking_count = 0
    locked.final_snapshot = {
        "billing_month": locked.billing_month,
        "business_idempotency_key": business_idempotency_key,
        "control_version": locked.control_version,
        "evidence_hash": finalization_evidence_hash(evidences),
        "executing_attempt_id": executing_attempt_id,
        "executing_proposal_id": executing_proposal_id,
        "approved_finalization": expected_finalization_snapshot,
        "request_id": request_id,
        "steps": [
            {
                "step_key": item.step_key,
                "evidence_hash": item.evidence_hash,
                "evidence": item.snapshot,
            }
            for item in evidences
        ],
    }
    await log_action_tx(
        db,
        current_actor.user_id,
        "monthly_close.finalize",
        "monthly_close",
        locked.cycle_id,
        before_data={"status": prior_status},
        after_data={
            **({"status": "completed", **expected_finalization_snapshot}),
            "billing_month": locked.billing_month,
            "business_idempotency_key": business_idempotency_key,
            "evidence_hash": locked.final_snapshot["evidence_hash"],
            "executing_attempt_id": executing_attempt_id,
            "executing_proposal_id": executing_proposal_id,
            "request_id": request_id,
        },
    )
    await db.flush()
    return locked


async def load_finalize_context(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    _commands: Sequence[dict[str, Any]],
    *,
    evidence_loader: EvidenceLoader | None = None,
):
    from app.services.monthly_close.control import ProposalFreshnessContext

    snapshot = await _finalization_live_snapshot(
        db, cycle, evidence_loader=evidence_loader
    )
    aggregate = _digest(snapshot)
    return ProposalFreshnessContext(
        evidence_hash=aggregate,
        subject_versions={
            cycle.cycle_id: cycle.control_version,
            **snapshot["evidence_hashes"],
        },
        active_input_set_hash=aggregate,
        mapping_versions={},
        ruleset_version="monthly-close-finalization-rules-v2",
        calculation_version="monthly-close-finalization-v2",
        configuration_snapshot_hash=_digest(
            {
                "billing_month": cycle.billing_month,
                "cycle_id": cycle.cycle_id,
                "preflight_hash": snapshot["preflight_hash"],
            }
        ),
        control_version=cycle.control_version,
    )


async def execute_finalize_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: MonthlyCloseProposal,
    command: dict[str, Any],
    actor: User,
    request_id: str,
) -> dict[str, Any]:
    expected_hashes = command["after"].get("evidence_hashes")
    expected_version = command["after"].get("control_version")
    if not isinstance(expected_hashes, dict) or not isinstance(expected_version, int):
        raise MonthlyCloseConflict(
            "finalization_command_invalid",
            "关账命令缺少固定的控制版本或九项证据哈希",
        )
    attempt_id = await db.scalar(
        select(MonthlyCloseExecutionAttempt.attempt_id)
        .where(
            MonthlyCloseExecutionAttempt.cycle_id == cycle.cycle_id,
            MonthlyCloseExecutionAttempt.proposal_id == proposal.proposal_id,
            MonthlyCloseExecutionAttempt.request_id == request_id,
            MonthlyCloseExecutionAttempt.status == "executing",
            MonthlyCloseExecutionAttempt.proposal_binding_hash
            == proposal.proposal_binding_hash,
        )
        .with_for_update()
    )
    if attempt_id is None:
        raise MonthlyCloseConflict(
            "finalization_attempt_lineage_invalid",
            "关账执行记录归属无效，不能执行关账",
        )
    completed = await finalize_monthly_close(
        db,
        cycle,
        actor,
        request_id=request_id,
        business_idempotency_key=command["business_idempotency_key"],
        executing_attempt_id=attempt_id,
        executing_proposal_id=proposal.proposal_id,
        expected_control_version=expected_version,
        expected_evidence_hashes={str(key): str(value) for key, value in expected_hashes.items()},
        expected_finalization_snapshot={
            key: value
            for key, value in command["after"].items()
            if key != "status"
        },
    )
    return {
        "result": {
            "billing_month": completed.billing_month,
            "status": completed.status,
        },
        "audit_refs": [f"monthly_close.finalize:{completed.cycle_id}:{request_id}"],
    }


async def verify_finalize_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: MonthlyCloseProposal,
    command: dict[str, Any],
    attempt: MonthlyCloseExecutionAttempt,
) -> dict[str, Any]:
    await db.refresh(cycle)
    snapshot = cycle.final_snapshot if isinstance(cycle.final_snapshot, dict) else {}
    expected_hashes = command["after"].get("evidence_hashes")
    expected_control_version = command["after"].get("control_version")
    actual_hashes = {
        item.get("step_key"): item.get("evidence_hash")
        for item in snapshot.get("steps", [])
        if isinstance(item, dict)
    }
    live_evidences = await build_step_evidences(db, cycle)
    live_hashes = {
        item.step_key: item.evidence_hash for item in live_evidences
    }
    live_gates_clear = (
        len(live_evidences) == len(MONTHLY_CLOSE_STEP_KEYS)
        and [item.step_key for item in live_evidences]
        == list(MONTHLY_CLOSE_STEP_KEYS)
        and all(item.blocking_count == 0 for item in live_evidences)
        and live_hashes == expected_hashes
    )
    confirmation_hashes = compatible_confirmation_hashes(live_evidences, {
        item.step_key: item
        for item in await db.scalars(
            select(MonthlyCloseStepConfirmation).where(
                MonthlyCloseStepConfirmation.cycle_id == cycle.cycle_id
            )
        )
    })
    open_issue_count = int(
        await db.scalar(
            select(func.count())
            .select_from(MonthlyCloseIssueInstance)
            .where(
                MonthlyCloseIssueInstance.cycle_id == cycle.cycle_id,
                MonthlyCloseIssueInstance.status.not_in(("resolved", "cancelled")),
            )
        )
        or 0
    )
    open_remediation_count = int(
        await db.scalar(
            select(func.count())
            .select_from(MonthlyCloseRemediation)
            .where(
                MonthlyCloseRemediation.cycle_id == cycle.cycle_id,
                MonthlyCloseRemediation.status != "resolved",
            )
        )
        or 0
    )
    unsafe_attempt_count = int(
        await db.scalar(
            select(func.count())
            .select_from(MonthlyCloseExecutionAttempt)
            .where(
                MonthlyCloseExecutionAttempt.cycle_id == cycle.cycle_id,
                MonthlyCloseExecutionAttempt.attempt_id != attempt.attempt_id,
                MonthlyCloseExecutionAttempt.status.not_in(
                    ("failed_safe", "failed_confirmed", "verified")
                ),
            )
        )
        or 0
    )
    audits = list(
        await db.scalars(
            select(AuditLog).where(
                AuditLog.action == "monthly_close.finalize",
                AuditLog.resource_id == cycle.cycle_id,
            )
        )
    )
    audit_present = any(
        isinstance(audit.after_data, dict)
        and audit.before_data == command["before"]
        and audit.after_data.get("request_id") == attempt.request_id
        and audit.after_data.get("business_idempotency_key")
        == command["business_idempotency_key"]
        and all(
            audit.after_data.get(key) == value
            for key, value in command["after"].items()
        )
        for audit in audits
    )
    idempotency_present = (
        snapshot.get("request_id") == attempt.request_id
        and snapshot.get("business_idempotency_key")
        == command["business_idempotency_key"]
        and audit_present
    )
    target_matches = (
        cycle.status == "completed"
        and command["subject_id"] == cycle.cycle_id
        and actual_hashes == expected_hashes
        and live_gates_clear
        and confirmation_hashes == live_hashes
        and snapshot.get("control_version") == expected_control_version
        and cycle.control_version == expected_control_version
        and attempt.cycle_id == cycle.cycle_id
        and attempt.proposal_id == proposal.proposal_id
        and attempt.proposal_binding_hash == proposal.proposal_binding_hash
    )
    all_gates_clear = (
        target_matches
        and audit_present
        and idempotency_present
        and open_issue_count == 0
        and open_remediation_count == 0
        and unsafe_attempt_count == 0
    )
    outcome = "applied" if all_gates_clear else "inconclusive"
    verification_evidence_hash = _digest(
        {
            "actual_hashes": actual_hashes,
            "audit_present": audit_present,
            "confirmation_hashes": confirmation_hashes,
            "idempotency_present": idempotency_present,
            "live_hashes": live_hashes,
            "open_issue_count": open_issue_count,
            "open_remediation_count": open_remediation_count,
            "target_matches": target_matches,
            "unsafe_attempt_count": unsafe_attempt_count,
        }
    )
    return {
        "outcome": outcome,
        "business_idempotency_key": command["business_idempotency_key"],
        "subject": {
            "subject_id": command["subject_id"],
            "expected_version": proposal.subject_versions.get(
                command["subject_id"], proposal.subject_versions.get("cycle")
            ),
            "actual_version": cycle.control_version,
        },
        "target": {
            "expected_before": command["before"],
            "expected_after": command["after"],
            "actual": command["after"] if target_matches else {
                "control_version": cycle.control_version,
                "evidence_hashes": live_hashes,
                "status": cycle.status,
            },
        },
        "amount": {
            "expected": command["amount_impact"],
            "actual": command["amount_impact"] if target_matches else "0.00",
        },
        "audit": {
            "present": audit_present,
            "request_id": attempt.request_id,
            "business_idempotency_key": command["business_idempotency_key"],
            "expected_before": command["before"],
            "expected_after": command["after"],
        },
        "idempotency": {
            "present": idempotency_present,
            "request_id": attempt.request_id,
            "business_idempotency_key": command["business_idempotency_key"],
        },
        "evidence": {
            "approved_evidence_hash": proposal.evidence_hash,
            "verification_evidence_hash": verification_evidence_hash,
        },
        "issues": [],
    }
