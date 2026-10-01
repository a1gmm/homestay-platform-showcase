"""Idempotent administrator alerts for stuck or stale monthly closes."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.monthly_close import (
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseInboxItem,
)
from app.models.monthly_close_control import (
    MonthlyCloseApproval,
    MonthlyCloseApprovalEvaluation,
    MonthlyCloseDocumentAnalysis,
    MonthlyCloseEvent,
    MonthlyCloseExecutionAttempt,
    MonthlyCloseOutbox,
    MonthlyCloseProposal,
    MonthlyCloseRemediation,
    MonthlyCloseRun,
    MonthlyCloseVerification,
)
from app.models.notification import Notification, NotificationLog, NotificationType
from app.models.user import User, UserRole
from app.services.monthly_close.workflow import build_cycle_summary


_SOURCE_LABELS = {
    "cleaning_statement": "保洁打扫记录",
    "linen_statement": "布草／洗涤记录",
    "utility_expense": "水电支出",
    "utility_receipt": "水电支出",
    "ota_statement": "OTA 平台账单",
    "operating_expenses": "其他运营支出",
}


@dataclass(frozen=True)
class MonthlyCloseMetricSample:
    """One low-cardinality aggregate ready for the configured metrics sink."""

    name: str
    value: float
    labels: dict[str, str]


_ALLOWED_METRIC_LABELS: dict[str, frozenset[str]] = {
    "owner": frozenset({"legacy", "assistant"}),
    "source_type": frozenset(
        {
            "cleaning_statement",
            "linen_statement",
            "utility_receipt",
            "utility_expense",
            "ota_statement",
            "operating_expenses",
            "none",
        }
    ),
    "status": frozenset(
        {
            "active",
            "approved",
            "archived",
            "available",
            "cancelled",
            "completed",
            "compensating",
            "dead_letter",
            "degraded",
            "disabled",
            "draft",
            "executing",
            "failed",
            "failed_confirmed",
            "failed_safe",
            "healthy",
            "inconclusive",
            "investigating",
            "leased",
            "open",
            "passed",
            "pending",
            "pending_approval",
            "processed",
            "processing",
            "queued",
            "rejected",
            "remediation_required",
            "reopened",
            "resolved",
            "rework",
            "running",
            "stored",
            "stale",
            "succeeded",
            "succeeded_unverified",
            "superseded",
            "unknown",
            "verified",
            "waiting_approval",
            "waiting_user",
        }
    ),
    "proposal_type": frozenset(
        {
            "ota_reconciliation",
            "ota_appeal_adjudication",
            "service_fee_reconciliation",
            "utility_reconciliation",
            "operating_expense_import",
            "issue_resolution",
            "generate_owner_settlements",
            "finalize_monthly_close",
        }
    ),
    "subsystem": frozenset(
        {
            "manual_path",
            "natural_language",
            "model",
            "source_adapters",
            "proposal_execution",
            "low_risk_automation",
            "outbox",
            "verification",
            "finalization",
        }
    ),
}


def validate_monthly_close_metric_labels(labels: dict[str, str]) -> None:
    """Reject unbounded or identifying labels before they reach a metrics sink."""
    for key, value in labels.items():
        allowed = _ALLOWED_METRIC_LABELS.get(key)
        if allowed is None or not isinstance(value, str) or value not in allowed:
            raise ValueError("monthly-close metric label is not allowlisted")


def _metric(
    name: str, value: int | float, **labels: str
) -> MonthlyCloseMetricSample:
    validate_monthly_close_metric_labels(labels)
    return MonthlyCloseMetricSample(name=name, value=float(value), labels=labels)


def _seconds_since(value: datetime | None, now: datetime) -> float:
    if value is None:
        return 0.0
    aware = value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)
    return max(0.0, (now - aware).total_seconds())


def _as_utc(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=timezone.utc)
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _verification_backlog_attempt_ids(
    attempts: list[MonthlyCloseExecutionAttempt],
    verifications: list[MonthlyCloseVerification],
) -> set[str]:
    latest: dict[tuple[str, str], MonthlyCloseVerification] = {}
    for verification in verifications:
        key = (verification.cycle_id, verification.attempt_id)
        current = latest.get(key)
        if current is None or (
            _as_utc(verification.verified_at or verification.created_at),
            verification.verification_id,
        ) > (
            _as_utc(current.verified_at or current.created_at),
            current.verification_id,
        ):
            latest[key] = verification
    backlog: set[str] = set()
    for attempt in attempts:
        current = latest.get((attempt.cycle_id, attempt.attempt_id))
        if attempt.status in {"unknown", "remediation_required"}:
            backlog.add(attempt.attempt_id)
        elif attempt.status == "succeeded_unverified" and (
            current is None or current.status != "passed"
        ):
            backlog.add(attempt.attempt_id)
        if current is not None and current.status in {
            "pending",
            "running",
            "failed",
            "inconclusive",
        }:
            backlog.add(attempt.attempt_id)
    return backlog


def _approval_satisfied_at(
    proposal: MonthlyCloseProposal,
    approvals: list[MonthlyCloseApproval],
    evaluations: list[MonthlyCloseApprovalEvaluation],
) -> datetime | None:
    policy = proposal.approval_policy_snapshot or {}
    required = int(policy.get("required_approvals") or 0)
    policy_version = policy.get("policy_version")
    matching = sorted(
        (
            item
            for item in approvals
            if item.proposal_id == proposal.proposal_id
            and item.decision == "approved"
            and item.proposal_binding_hash == proposal.proposal_binding_hash
            and item.policy_version == policy_version
            and item.required_approvals == required
        ),
        key=lambda item: (item.sequence_no, item.approval_id),
    )
    if required < 1 or len(matching) < required:
        return None
    required_rows = matching[:required]
    required_ids = [item.approval_id for item in required_rows]
    candidates = [
        item
        for item in evaluations
        if item.proposal_id == proposal.proposal_id
        and item.satisfied
        and item.policy_version == policy_version
        and item.proposal_binding_hash == proposal.proposal_binding_hash
        and all(approval_id in item.approval_ids for approval_id in required_ids)
    ]
    evaluation_time = max(
        (_as_utc(item.evaluated_at) for item in candidates),
        default=datetime.min.replace(tzinfo=timezone.utc),
    )
    decision_time = max(_as_utc(item.decided_at) for item in required_rows)
    return max(evaluation_time, decision_time)


async def collect_monthly_close_metrics(
    db: AsyncSession, *, now: datetime | None = None
) -> list[MonthlyCloseMetricSample]:
    """Build bounded aggregate metrics without filenames, IDs, hashes or free text."""
    now = now or datetime.now(timezone.utc)
    documents = list(await db.scalars(select(MonthlyCloseDocument)))
    inbox_items = list(await db.scalars(select(MonthlyCloseInboxItem)))
    analyses = list(await db.scalars(select(MonthlyCloseDocumentAnalysis)))
    proposals = list(await db.scalars(select(MonthlyCloseProposal)))
    approvals = list(await db.scalars(select(MonthlyCloseApproval)))
    evaluations = list(await db.scalars(select(MonthlyCloseApprovalEvaluation)))
    attempts = list(await db.scalars(select(MonthlyCloseExecutionAttempt)))
    verifications = list(await db.scalars(select(MonthlyCloseVerification)))
    remediations = list(await db.scalars(select(MonthlyCloseRemediation)))
    outbox_rows = list(await db.scalars(select(MonthlyCloseOutbox)))
    runs = list(await db.scalars(select(MonthlyCloseRun)))
    cycles = list(await db.scalars(select(MonthlyCloseCycle)))

    samples: list[MonthlyCloseMetricSample] = []
    processing_counts = Counter(
        (item.source_type or "none", item.processing_status) for item in documents
    )
    for (source_type, status), count in sorted(processing_counts.items()):
        samples.append(
            _metric(
                "monthly_close_processing_total",
                count,
                source_type=source_type,
                status=status,
            )
        )
    receipt_ages: dict[str, float] = {}
    receipt_by_document = {
        item.document_id: item
        for item in inbox_items
        if item.document_id is not None
    }
    for item in documents:
        receipt = receipt_by_document.get(item.document_id)
        if receipt is None:
            continue
        source_type = item.source_type or "none"
        received = _as_utc(receipt.created_at)
        stored = _as_utc(item.uploaded_at)
        receipt_ages[source_type] = max(
            receipt_ages.get(source_type, 0.0),
            max(0.0, (stored - received).total_seconds()),
        )
    if not receipt_ages:
        receipt_ages["none"] = 0.0
    for source_type, age in sorted(receipt_ages.items()):
        samples.append(
            _metric(
                "monthly_close_receipt_latency_seconds",
                age,
                source_type=source_type,
            )
        )
    uploaded_at_by_document = {
        item.document_id: item.uploaded_at for item in documents
    }
    analysis_latencies: dict[str, float] = {}
    for analysis in analyses:
        uploaded_at = uploaded_at_by_document.get(analysis.document_id)
        if uploaded_at is None:
            continue
        source_type = analysis.source_type or "none"
        uploaded = (
            uploaded_at
            if uploaded_at.tzinfo is not None
            else uploaded_at.replace(tzinfo=timezone.utc)
        )
        completed = analysis.completed_at or now
        completed = (
            completed
            if completed.tzinfo is not None
            else completed.replace(tzinfo=timezone.utc)
        )
        analysis_latencies[source_type] = max(
            analysis_latencies.get(source_type, 0.0),
            max(0.0, (completed - uploaded).total_seconds()),
        )
    if not analysis_latencies:
        analysis_latencies["none"] = 0.0
    for source_type, latency in sorted(analysis_latencies.items()):
        samples.append(
            _metric(
                "monthly_close_analysis_latency_seconds",
                latency,
                source_type=source_type,
            )
        )

    for (proposal_type, status), count in sorted(
        Counter((item.proposal_type, item.status) for item in proposals).items()
    ):
        samples.append(
            _metric(
                "monthly_close_proposal_total",
                count,
                proposal_type=proposal_type,
                status=status,
            )
        )
    for status, count in sorted(Counter(item.status for item in attempts).items()):
        samples.append(_metric("monthly_close_attempt_total", count, status=status))
    for status, count in sorted(
        Counter(item.status for item in verifications).items()
    ):
        samples.append(
            _metric("monthly_close_verification_total", count, status=status)
        )
    for status, count in sorted(
        Counter(item.status for item in remediations).items()
    ):
        samples.append(
            _metric("monthly_close_remediation_total", count, status=status)
        )
    for status, count in sorted(Counter(item.status for item in runs).items()):
        samples.append(
            _metric("monthly_close_assistant_run_total", count, status=status)
        )
    for status, count in sorted(
        Counter(item.status for item in outbox_rows).items()
    ):
        samples.append(_metric("monthly_close_outbox_total", count, status=status))

    attempted_proposals = {item.proposal_id for item in attempts}
    stale_approval_count = 0
    for item in proposals:
        if item.status != "approved" or item.proposal_id in attempted_proposals:
            continue
        satisfied_at = _approval_satisfied_at(item, approvals, evaluations)
        if satisfied_at is not None and _seconds_since(satisfied_at, now) >= 3600:
            stale_approval_count += 1
    unknown_count = sum(item.status == "unknown" for item in attempts)
    outbox_backlog_count = sum(
        item.status in {"pending", "leased", "failed", "dead_letter"}
        for item in outbox_rows
    )
    dead_letter_count = sum(item.status == "dead_letter" for item in outbox_rows)
    outbox_lag = max(
        (
            _seconds_since(item.available_at, now)
            for item in outbox_rows
            if item.status in {"pending", "leased", "failed", "dead_letter"}
        ),
        default=0.0,
    )
    finalization_ids = {
        item.proposal_id
        for item in proposals
        if item.proposal_type == "finalize_monthly_close"
    }
    finalization_failure_count = sum(
        item.proposal_id in finalization_ids
        and item.status
        in {"failed_safe", "failed_confirmed", "unknown", "remediation_required"}
        for item in attempts
    )
    verification_backlog_count = len(
        _verification_backlog_attempt_ids(attempts, verifications)
    )
    samples.extend(
        [
            _metric("monthly_close_stale_approval_total", stale_approval_count),
            _metric("monthly_close_unknown_attempt_total", unknown_count),
            _metric(
                "monthly_close_verification_backlog_total",
                verification_backlog_count,
            ),
            _metric("monthly_close_outbox_lag_seconds", outbox_lag),
            _metric("monthly_close_outbox_dead_letter_total", dead_letter_count),
            _metric(
                "monthly_close_finalization_failure_total",
                finalization_failure_count,
            ),
        ]
    )
    for owner, count in sorted(
        Counter(item.write_control_owner for item in cycles).items()
    ):
        samples.append(
            _metric("monthly_close_write_owner_cycles", count, owner=owner)
        )
    switch_count = int(
        await db.scalar(
            select(func.count(MonthlyCloseEvent.event_id)).where(
                MonthlyCloseEvent.event_type == "write_control.switched"
            )
        )
        or 0
    )
    samples.append(_metric("monthly_close_owner_switch_total", switch_count))
    subsystem_statuses = {
        "manual_path": "healthy",
        "natural_language": (
            "available" if settings.MONTHLY_CLOSE_ASSISTANT_ENABLED else "disabled"
        ),
        "model": (
            "available"
            if settings.MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED
            else "disabled"
        ),
        "source_adapters": (
            "available"
            if settings.MONTHLY_CLOSE_SOURCE_ADAPTERS_ENABLED
            else "disabled"
        ),
        "proposal_execution": (
            "available"
            if settings.MONTHLY_CLOSE_PROPOSAL_EXECUTION_ENABLED
            else "disabled"
        ),
        "low_risk_automation": (
            "available"
            if settings.MONTHLY_CLOSE_LOW_RISK_AUTOMATION_ENABLED
            else "disabled"
        ),
        "outbox": "degraded" if outbox_backlog_count else "available",
        "verification": (
            "degraded"
            if unknown_count or verification_backlog_count
            else "available"
        ),
        "finalization": (
            "degraded" if finalization_failure_count else "available"
        ),
    }
    samples.extend(
        _metric(
            "monthly_close_subsystem_status",
            1,
            subsystem=subsystem,
            status=status,
        )
        for subsystem, status in sorted(subsystem_statuses.items())
    )
    return samples


def _subsystem(flag: bool) -> dict[str, Any]:
    return {"enabled": flag, "status": "available" if flag else "disabled"}


async def monthly_close_rollout_health(db: AsyncSession) -> dict[str, Any]:
    """Readiness view that never lets manual availability mask assistant backlog."""
    outbox_pending = int(
        await db.scalar(
            select(func.count(MonthlyCloseOutbox.outbox_id)).where(
                MonthlyCloseOutbox.status.in_(
                    ("pending", "leased", "failed", "dead_letter")
                )
            )
        )
        or 0
    )
    unknown_attempts = int(
        await db.scalar(
            select(func.count(MonthlyCloseExecutionAttempt.attempt_id)).where(
                MonthlyCloseExecutionAttempt.status.in_(
                    ("unknown", "remediation_required")
                )
            )
        )
        or 0
    )
    attempts = list(await db.scalars(select(MonthlyCloseExecutionAttempt)))
    verifications = list(await db.scalars(select(MonthlyCloseVerification)))
    verification_backlog = len(
        _verification_backlog_attempt_ids(attempts, verifications)
    )
    finalization_failures = int(
        await db.scalar(
            select(func.count(MonthlyCloseExecutionAttempt.attempt_id))
            .join(
                MonthlyCloseProposal,
                MonthlyCloseProposal.proposal_id
                == MonthlyCloseExecutionAttempt.proposal_id,
            )
            .where(
                MonthlyCloseProposal.proposal_type == "finalize_monthly_close",
                MonthlyCloseExecutionAttempt.status.in_(
                    (
                        "failed_safe",
                        "failed_confirmed",
                        "unknown",
                        "remediation_required",
                    )
                ),
            )
        )
        or 0
    )
    assistant = {
        "natural_language": _subsystem(
            bool(settings.MONTHLY_CLOSE_ASSISTANT_ENABLED)
        ),
        "external_intake": _subsystem(
            bool(settings.MONTHLY_CLOSE_EXTERNAL_INTAKE_ENABLED)
        ),
        "model": _subsystem(bool(settings.MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED)),
        "source_adapters": _subsystem(
            bool(settings.MONTHLY_CLOSE_SOURCE_ADAPTERS_ENABLED)
        ),
        "proposal_execution": _subsystem(
            bool(settings.MONTHLY_CLOSE_PROPOSAL_EXECUTION_ENABLED)
        ),
        "low_risk_automation": _subsystem(
            bool(settings.MONTHLY_CLOSE_LOW_RISK_AUTOMATION_ENABLED)
        ),
        "outbox": {
            "status": "degraded" if outbox_pending else "available",
            "backlog": outbox_pending,
        },
        "verification": {
            "status": (
                "degraded"
                if unknown_attempts or verification_backlog
                else "available"
            ),
            "backlog": verification_backlog,
            "unknown": unknown_attempts,
        },
        "finalization": {
            "status": "degraded" if finalization_failures else "available",
            "failures": finalization_failures,
        },
    }
    degraded = any(
        value.get("status") == "degraded" for value in assistant.values()
    )
    return {
        "overall": "degraded" if degraded else "healthy",
        "manual_path": {"available": True, "status": "healthy"},
        "assistant": assistant,
        "backlogs": {
            "outbox_pending": outbox_pending,
            "unknown_attempts": unknown_attempts,
            "verification": verification_backlog,
            "finalization_failures": finalization_failures,
        },
    }


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _fingerprint(summary: dict) -> str:
    payload = {
        "status": summary["status"],
        "current_step": summary["current_step"],
        "blocking_count": summary["blocking_count"],
        "missing_sources": summary["missing_sources"],
        "inbox_pending_count": summary["inbox_pending_count"],
        "current_evidence_hash": summary.get("current_evidence_hash"),
        "blocking_issues": summary.get("blocking_issues", []),
    }
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


def _reminder_dedupe_hash(
    *,
    cycle_id: str,
    recipient: str,
    scope: str,
    reason: str,
    state_hash: str,
) -> str:
    """Stable database identity for one recipient and one exact stuck state."""
    return sha256(
        json.dumps(
            {
                "cycle_id": cycle_id,
                "recipient": recipient,
                "scope": scope,
                "reason": reason,
                "state_hash": state_hash,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


async def scan_monthly_close_alerts(db: AsyncSession) -> int:
    """Create one notification per admin and evidence state, never mutate a cycle."""
    admins = list(
        (
            await db.execute(
                select(User).where(
                    User.role == UserRole.admin,
                    User.is_active.is_(True),
                )
            )
        ).scalars()
    )
    if not admins:
        return 0
    cycles = list((await db.execute(select(MonthlyCloseCycle))).scalars())
    now = datetime.now(timezone.utc)
    created = 0
    state_changed = False
    for cycle in cycles:
        summary = await build_cycle_summary(db, cycle)
        monitor_values = (
            summary["status"],
            summary["current_step"],
            summary["progress"],
            summary["blocking_count"],
        )
        stored_monitor_values = (
            cycle.monitor_effective_status,
            cycle.monitor_current_step,
            cycle.monitor_progress,
            cycle.monitor_blocking_count,
        )
        if monitor_values != stored_monitor_values:
            (
                cycle.monitor_effective_status,
                cycle.monitor_current_step,
                cycle.monitor_progress,
                cycle.monitor_blocking_count,
            ) = monitor_values
            state_changed = True
        reason: str | None = None
        title = ""
        content = ""
        if summary["status"] == "needs_recheck":
            reason = "needs_recheck"
            title = f"{cycle.billing_month} 月结完成后数据发生变化"
            content = (
                f"当前需要从“{summary['current_step_label']}”重新核对。"
                "原完成快照仍保留，请进入月结中心查看变化并填写原因重新打开。"
            )
        elif cycle.status != "completed":
            actionable = bool(
                summary["missing_source_count"]
                or summary["blocking_count"]
                or summary["inbox_pending_count"]
            )
            state_hash = _fingerprint(summary) if actionable else None
            if state_hash != cycle.monitor_state_hash:
                cycle.monitor_state_hash = state_hash
                cycle.monitor_state_since = now if actionable else None
                state_changed = True
            elif (
                actionable
                and cycle.monitor_state_since is not None
                and _utc(cycle.monitor_state_since) <= now - timedelta(days=1)
            ):
                reason = "stuck"
                title = f"{cycle.billing_month} 月结仍有待处理事项"
                parts = []
                if summary["missing_source_count"]:
                    missing_labels = [
                        _SOURCE_LABELS.get(source_type, "其他资料")
                        for source_type in summary["missing_sources"]
                    ]
                    parts.append(f"缺少：{'、'.join(missing_labels)}")
                if summary["inbox_pending_count"]:
                    parts.append(f"收件箱 {summary['inbox_pending_count']} 个文件待确认")
                if summary["blocking_count"]:
                    parts.append(f"当前步骤 {summary['blocking_count']} 个阻断项")
                content = "，".join(parts) + "。进入月结中心后按当前步骤继续处理。"
        if reason is None:
            continue
        state_hash = _fingerprint(summary)
        action_url = f"/finance/monthly-close?month={cycle.billing_month}"
        for admin in admins:
            scope = "admin_cycle"
            marker_key = "mc:" + sha256(
                f"{cycle.cycle_id}:{admin.user_id}:{scope}:{reason}".encode()
            ).hexdigest()[:32]
            dedupe_hash = _reminder_dedupe_hash(
                cycle_id=cycle.cycle_id,
                recipient=admin.user_id,
                scope=scope,
                reason=reason,
                state_hash=state_hash,
            )
            try:
                async with db.begin_nested():
                    # A deterministic primary key makes overlapping Celery scans
                    # serialize at the database boundary. Only the winner can
                    # insert the paired notification in this savepoint.
                    db.add(
                        NotificationLog(
                            log_id="MCN" + dedupe_hash[:17].upper(),
                            template_name=marker_key,
                            channel="in_app",
                            recipient=admin.user_id,
                            content=state_hash,
                            status="sent",
                        )
                    )
                    db.add(
                        Notification(
                            notification_id="NTF" + dedupe_hash[:16].upper(),
                            user_id=admin.user_id,
                            title=title,
                            content=content,
                            action_url=action_url,
                            type=NotificationType.alert,
                            is_read=False,
                        )
                    )
                    await db.flush()
            except IntegrityError:
                continue
            created += 1
    if created or state_changed:
        await db.commit()
    return created
