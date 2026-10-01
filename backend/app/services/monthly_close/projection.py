"""Role-scoped monthly-close workspace projection.

The projection is deliberately read-only.  It translates the durable receipt,
classification, analysis, requirement, and legacy workflow facts into the one
workspace contract consumed by both UI and assistant callers.

The deployed application is single-property: ``cycle_id`` is the existing
business boundary.  This service enforces cycle and actor visibility but does
not claim or invent a separate store-tenant permission model.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Literal

from sqlalchemy import exists, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.monthly_close import (
    MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES,
    MONTHLY_CLOSE_SOURCE_TYPES,
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseInboxItem,
    MonthlyCloseServiceLine,
    MonthlyCloseSourceRequirement,
)
from app.models.monthly_close_control import (
    MonthlyCloseDocumentAnalysis,
    MonthlyCloseExecutionAttempt,
    MonthlyCloseIssueInstance,
    MonthlyCloseProcessingJob,
    MonthlyCloseProposal,
    MonthlyCloseRemediation,
)
from app.models.settlement import OwnerSettlement
from app.models.user import User, UserRole
from app.services.monthly_close.evidence import build_step_evidences
from app.services.monthly_close.workflow import (
    build_effective_steps,
    completed_cycle_is_fresh,
)


SourceState = Literal[
    "missing",
    "processing",
    "needs_action",
    "completed",
    "blocked",
    "not_applicable",
]

PROJECTION_VERSION = "monthly-close-projection/v1"
EMPLOYEE_ROLES = frozenset({"admin", "finance", "operator", "cleaner", "keeper"})
FINANCIAL_DETAIL_ROLES = frozenset({"admin", "finance"})
ROLE_SOURCE_TYPES: dict[str, tuple[str, ...]] = {
    "admin": MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES,
    "finance": MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES,
    "operator": MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES,
    "cleaner": ("cleaning_statement",),
    "keeper": ("cleaning_statement", "linen_statement"),
}
ROLE_DOCUMENT_SOURCE_TYPES: dict[str, tuple[str, ...]] = {
    **ROLE_SOURCE_TYPES,
    "admin": MONTHLY_CLOSE_SOURCE_TYPES,
    "finance": MONTHLY_CLOSE_SOURCE_TYPES,
    "operator": MONTHLY_CLOSE_SOURCE_TYPES,
}


@dataclass(frozen=True)
class RecommendedAction:
    kind: str
    label: str
    reason_code: str
    target_type: str
    target_id: str


@dataclass(frozen=True)
class ProjectedDocument:
    document_id: str
    source_type: str
    filename: str
    storage_state: str
    classification_state: str
    analysis_state: str
    uploaded_at: str | None
    receipt_id: str | None = None
    byte_size: int | None = None
    sha256: str | None = None
    engine_type: str | None = None
    engine_id: str | None = None
    analysis_result: dict[str, Any] | None = None
    analysis_error: str | None = None
    work_record_count: int = 0
    analysis_kind: str | None = None
    analysis_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {key: value for key, value in asdict(self).items() if value is not None}

    def to_summary_dict(self) -> dict[str, Any]:
        return {
            "document_id": self.document_id,
            "source_type": self.source_type,
            "filename": self.filename,
            "storage_state": self.storage_state,
            "classification_state": self.classification_state,
            "analysis_state": self.analysis_state,
            **({"analysis_kind": self.analysis_kind} if self.analysis_kind else {}),
            **({"analysis_message": self.analysis_message} if self.analysis_message else {}),
            **({"work_record_count": self.work_record_count} if self.work_record_count else {}),
            **(
                {"uploaded_at": self.uploaded_at}
                if self.uploaded_at is not None
                else {}
            ),
            **({"receipt_id": self.receipt_id} if self.receipt_id is not None else {}),
        }


@dataclass(frozen=True)
class FinalReviewProjection:
    state: str
    source_complete_count: int
    source_total_count: int
    unresolved_issue_count: int
    settlement_count: int
    settlement_total_amount: str
    approval_state: str
    proposal_id: str | None
    attempt_id: str | None
    confirmed_settlement_count: int = 0
    paid_settlement_count: int = 0
    settlement_status_message: str = ""
    settlements: tuple[dict[str, str], ...] = ()


@dataclass(frozen=True)
class MonthlyCloseSourceProjection:
    source_id: str
    source_type: str
    state: SourceState
    documents: tuple[ProjectedDocument, ...]
    not_applicable_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "source_id": self.source_id,
            "source_type": self.source_type,
            "state": self.state,
            "documents": [document.to_summary_dict() for document in self.documents],
        }
        if self.not_applicable_reason is not None:
            result["not_applicable_reason"] = self.not_applicable_reason
        return result


@dataclass(frozen=True)
class MonthlyCloseProjection:
    projection_version: str
    computed_at: str
    input_hash: str
    cycle_id: str
    billing_month: str
    cycle_status: str
    actor_role: str
    features: dict[str, bool]
    can_advance_final_review: bool
    sources: tuple[MonthlyCloseSourceProjection, ...]
    workflow_evidence: list[dict[str, Any]]
    final_close_blockers: list[str]
    final_review: FinalReviewProjection
    recommended_action: RecommendedAction
    financial_case: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            **({"financial_case": self.financial_case} if self.financial_case is not None else {}),
            "projection_version": self.projection_version,
            "computed_at": self.computed_at,
            "input_hash": self.input_hash,
            "cycle_id": self.cycle_id,
            "billing_month": self.billing_month,
            "cycle_status": self.cycle_status,
            "actor_role": self.actor_role,
            "features": self.features,
            "can_advance_final_review": self.can_advance_final_review,
            "sources": [source.to_dict() for source in self.sources],
            "workflow_evidence": self.workflow_evidence,
            "final_close_blockers": self.final_close_blockers,
            "final_review": asdict(self.final_review),
            "recommended_action": asdict(self.recommended_action),
        }


def _actor_identity(actor: User | dict[str, Any]) -> tuple[str, str]:
    if isinstance(actor, dict):
        actor_id = str(actor.get("user_id") or "")
        role_value = actor.get("role")
    else:
        actor_id = str(getattr(actor, "user_id", ""))
        role_value = getattr(actor, "role", None)
    role = (
        role_value.value if isinstance(role_value, UserRole) else str(role_value or "")
    )
    if not actor_id or role not in EMPLOYEE_ROLES:
        raise ValueError("monthly-close projection requires an employee actor")
    return actor_id, role


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    normalized = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return normalized.isoformat()


async def _load_document_rows(
    db: AsyncSession,
    cycle_id: str,
    *,
    actor_id: str,
    actor_role: str,
    source_types: tuple[str, ...],
    document_id: str | None = None,
    include_detail: bool = False,
) -> list[Any]:
    can_view_all_uploaders = actor_role in {"admin", "finance", "operator"}
    include_privileged_detail = include_detail and can_view_all_uploaders
    own_receipt_id = (
        select(MonthlyCloseInboxItem.item_id)
        .where(
            MonthlyCloseInboxItem.cycle_id == cycle_id,
            MonthlyCloseInboxItem.document_id == MonthlyCloseDocument.document_id,
            MonthlyCloseInboxItem.created_by == actor_id,
        )
        .scalar_subquery()
        .label("receipt_id")
    )
    from app.models.cleaning_work_record import CleaningWorkRecord
    work_record_count = select(func.coalesce(func.sum(CleaningWorkRecord.quantity), 0)).select_from(CleaningWorkRecord).where(CleaningWorkRecord.document_id == MonthlyCloseDocument.document_id).scalar_subquery().label("work_record_count")
    columns = [
        work_record_count,
        MonthlyCloseDocument.document_id,
        MonthlyCloseDocument.source_type,
        MonthlyCloseDocument.filename,
        MonthlyCloseDocument.processing_status,
        MonthlyCloseDocument.uploaded_at,
        MonthlyCloseDocument.uploaded_by,
        own_receipt_id,
    ]
    if can_view_all_uploaders:
        columns.extend([
            MonthlyCloseDocument.metadata_.label("document_metadata"),
            MonthlyCloseDocument.sha256.label("recognition_sha256"),
        ])
    if include_privileged_detail:
        columns.extend(
            [
                MonthlyCloseDocument.byte_size,
                MonthlyCloseDocument.engine_type,
                MonthlyCloseDocument.engine_id,
                MonthlyCloseDocument.sha256,
            ]
        )
    statement = select(*columns).where(
        MonthlyCloseDocument.cycle_id == cycle_id,
        MonthlyCloseDocument.is_active.is_(True),
        MonthlyCloseDocument.source_type.in_(source_types),
    )
    if not can_view_all_uploaders:
        statement = statement.where(
            or_(
                MonthlyCloseDocument.uploaded_by == actor_id,
                exists(
                    select(MonthlyCloseInboxItem.item_id).where(
                        MonthlyCloseInboxItem.cycle_id == cycle_id,
                        MonthlyCloseInboxItem.document_id
                        == MonthlyCloseDocument.document_id,
                        MonthlyCloseInboxItem.created_by == actor_id,
                    )
                ),
            )
        )
    if document_id is not None:
        statement = statement.where(MonthlyCloseDocument.document_id == document_id)
    return list((await db.execute(statement)).mappings())


async def _load_latest_analyses(
    db: AsyncSession,
    *,
    cycle_id: str,
    document_ids: list[str],
    include_financial_detail: bool,
) -> dict[str, Any]:
    if not document_ids:
        return {}
    columns = [
        MonthlyCloseDocumentAnalysis.document_id,
        MonthlyCloseDocumentAnalysis.source_type,
        MonthlyCloseDocumentAnalysis.generation,
        MonthlyCloseDocumentAnalysis.classification_generation,
        MonthlyCloseDocumentAnalysis.status,
    ]
    if include_financial_detail:
        columns.extend(
            [
                MonthlyCloseDocumentAnalysis.manual_action,
                MonthlyCloseDocumentAnalysis.result,
            ]
        )
    rows = list(
        (
            await db.execute(
                select(*columns)
                .where(
                    MonthlyCloseDocumentAnalysis.cycle_id == cycle_id,
                    MonthlyCloseDocumentAnalysis.document_id.in_(document_ids),
                )
                .order_by(
                    MonthlyCloseDocumentAnalysis.document_id,
                    MonthlyCloseDocumentAnalysis.generation.desc(),
                )
            )
        ).mappings()
    )
    latest: dict[str, Any] = {}
    for row in rows:
        latest.setdefault(row["document_id"], row)
    return latest


async def _load_current_classification_generations(
    db: AsyncSession,
    *,
    cycle_id: str,
    document_ids: list[str],
) -> dict[str, int]:
    """Return the confirmed receipt generation linked to each archived document.

    Direct legacy uploads predate inbox receipts and therefore retain the
    original implicit generation 1. Once a receipt is linked, that durable
    generation is authoritative.
    """
    if not document_ids:
        return {}
    rows = list(
        (
            await db.execute(
                select(
                    MonthlyCloseInboxItem.document_id,
                    MonthlyCloseInboxItem.classification_generation,
                ).where(
                    MonthlyCloseInboxItem.cycle_id == cycle_id,
                    MonthlyCloseInboxItem.document_id.in_(document_ids),
                    MonthlyCloseInboxItem.status == "confirmed",
                )
            )
        ).mappings()
    )
    return {
        str(row["document_id"]): int(row["classification_generation"]) for row in rows
    }


async def _load_active_analysis_jobs(
    db: AsyncSession,
    *,
    cycle_id: str,
    document_ids: list[str],
) -> set[str]:
    if not document_ids:
        return set()
    return set(
        await db.scalars(
            select(MonthlyCloseProcessingJob.subject_id).where(
                MonthlyCloseProcessingJob.cycle_id == cycle_id,
                MonthlyCloseProcessingJob.subject_type == "document",
                MonthlyCloseProcessingJob.subject_id.in_(document_ids),
                MonthlyCloseProcessingJob.status.in_(("pending", "leased")),
            )
        )
    )


def _document_projection(
    row: Any,
    analysis: Any | None,
    *,
    job_active: bool,
    has_confirmed_receipt: bool,
    actor_role: str,
    projected_source_type: str | None = None,
) -> ProjectedDocument:
    processing_status = str(row["processing_status"])
    metadata = row.get("document_metadata") or {}
    recognition = metadata.get("work_log_recognition") or {}
    work_log = (str(row["source_type"]) == "cleaning_statement"
                and metadata.get("parser") == "cleaning_work_log_v1"
                and recognition.get("source_sha256") == row.get("recognition_sha256")
                and bool(recognition.get("source_sha256")))
    # A document row already owns the immutable bytes. Parser rejection is
    # an analysis failure, not a failed upload.
    storage_state = "stored"
    if work_log:
        classification_state = "confirmed"
        analysis_state = "work_log_ready"
    elif analysis is not None:
        classification_state = "confirmed"
        analysis_state = {
            "queued": "queued",
            "processing": "running",
            "needs_review": "needs_mapping",
            "completed": "ready",
            "failed_safe": "failed",
        }[str(analysis["status"])]
    elif processing_status == "rejected":
        classification_state = "confirmed"
        analysis_state = "failed"
    elif processing_status == "processed" and not has_confirmed_receipt:
        # Legacy/manual adapters predate the durable inbox analysis lineage.
        # Their deterministic success marker is the only completion evidence;
        # never use it when a receipt generation exists because that newer
        # control-plane analysis remains authoritative.
        classification_state = "confirmed"
        analysis_state = "ready"
    else:
        classification_state = "confirmed"
        analysis_state = "queued" if job_active else "not_started"
    include_file_metadata = actor_role in {"admin", "finance", "operator"}
    include_financial_detail = actor_role in FINANCIAL_DETAIL_ROLES
    return ProjectedDocument(
        document_id=str(row["document_id"]),
        source_type=projected_source_type or str(row["source_type"]),
        filename=str(row["filename"]),
        storage_state=storage_state,
        work_record_count=int(row.get("work_record_count") or 0) if actor_role == "admin" else 0,
        classification_state=classification_state,
        analysis_state=analysis_state,
        analysis_kind="cleaning_work_log" if work_log else None,
        analysis_message="已识别打扫记录，记录核对与费用核对分步进行。" if work_log else None,
        uploaded_at=_iso(row["uploaded_at"]),
        receipt_id=str(row["receipt_id"])
        if row.get("receipt_id") is not None
        else None,
        byte_size=int(row["byte_size"])
        if include_file_metadata and row.get("byte_size") is not None
        else None,
        sha256=str(row["sha256"])
        if include_file_metadata and row.get("sha256") is not None
        else None,
        engine_type=row.get("engine_type") if include_file_metadata else None,
        engine_id=row.get("engine_id") if include_file_metadata else None,
        analysis_result=(analysis.get("result") if analysis is not None else None)
        if include_financial_detail
        else None,
        analysis_error=(analysis.get("manual_action") if analysis is not None and not work_log else None)
        if include_financial_detail
        else None,
    )


def _workflow_evidence_projection(
    evidence: Any,
    effective: dict[str, Any],
    *,
    actor_role: str,
) -> dict[str, Any]:
    # Projection snapshots are polling/replay boundaries, not evidence payloads.
    # Detailed hashes, issues and financial rows are fetched only through an
    # actor-scoped selected-object endpoint.
    return {
        "step_key": evidence.step_key,
        "status": effective["status"],
        "blocking_count": evidence.blocking_count,
    }


def _source_state(
    requirement: Any,
    documents: tuple[ProjectedDocument, ...],
    *,
    inbox_facts: tuple[tuple[str, bool], ...],
    can_observe_global_requirement: bool,
    supplier_residual: bool = False,
) -> SourceState:
    if requirement["state"] == "not_applicable":
        return "not_applicable"
    inbox_statuses = tuple(status for status, _job_live in inbox_facts)
    if any(
        status in {"failed", "needs_review", "classified"} for status in inbox_statuses
    ):
        return "needs_action"
    if any(status == "received" and not job_live for status, job_live in inbox_facts):
        return "needs_action"
    if any(document.storage_state == "rejected" for document in documents):
        return "needs_action"
    if any(
        document.analysis_state in {"needs_mapping", "failed"} for document in documents
    ):
        return "needs_action"
    if any(document.analysis_state in {"queued", "running"} for document in documents):
        return "processing"
    if any(document.analysis_state == "not_started" for document in documents):
        return "needs_action"
    if any(document.analysis_state == "work_log_ready" for document in documents):
        return "needs_action"
    if supplier_residual:
        return "needs_action"
    if documents and all(document.analysis_state == "ready" for document in documents):
        return "completed"
    if requirement["state"] == "uploaded":
        # A global uploaded marker without an actor-visible document is not
        # evidence that this actor's work completed. Treat it like absence so
        # hidden and nonexistent objects have the same projection.
        return "blocked" if can_observe_global_requirement else "missing"
    if any(status == "received" and job_live for status, job_live in inbox_facts):
        return "processing"
    return "missing"


async def _final_review_projection(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    sources: tuple[MonthlyCloseSourceProjection, ...],
    *,
    include_financial_detail: bool,
) -> FinalReviewProjection:
    snapshot = cycle.final_snapshot if isinstance(cycle.final_snapshot, dict) else {}
    verified = snapshot.get("verified_finalization")
    latest_proposal = await db.scalar(
        select(MonthlyCloseProposal)
        .where(
            MonthlyCloseProposal.cycle_id == cycle.cycle_id,
            MonthlyCloseProposal.proposal_type == "finalize_monthly_close",
        )
        .order_by(
            MonthlyCloseProposal.proposal_version.desc(),
            MonthlyCloseProposal.created_at.desc(),
        )
        .limit(1)
    )
    state = "not_started"
    approval_state = "not_started"
    proposal_id = latest_proposal.proposal_id if latest_proposal is not None else None
    attempt_id = None
    if cycle.status == "reopened":
        state = "reopened"
    elif isinstance(verified, dict) and all(
        isinstance(verified.get(key), str) and verified.get(key)
        for key in (
            "attempt_id",
            "proposal_id",
            "verification_id",
            "verification_evidence_hash",
        )
    ):
        state = "verified"
        approval_state = "approved"
        proposal_id = str(verified["proposal_id"])
        attempt_id = str(verified["attempt_id"])
    elif latest_proposal is not None:
        approval_state = latest_proposal.status
        if latest_proposal.status == "pending_approval":
            state = "pending_approval"
        elif latest_proposal.status in {"stale", "superseded"}:
            state = "stale"
        elif latest_proposal.status == "rejected":
            state = "failed"
        elif latest_proposal.status == "approved":
            latest_attempt = await db.scalar(
                select(MonthlyCloseExecutionAttempt)
                .where(
                    MonthlyCloseExecutionAttempt.proposal_id
                    == latest_proposal.proposal_id
                )
                .order_by(
                    MonthlyCloseExecutionAttempt.attempt_no.desc(),
                    MonthlyCloseExecutionAttempt.created_at.desc(),
                )
                .limit(1)
            )
            state = (
                {
                    "pending": "approved",
                    "executing": "executing",
                    "succeeded_unverified": "succeeded_unverified",
                    "verified": "verifying",
                    "unknown": "unknown",
                    "remediation_required": "remediation",
                    "failed_safe": "failed",
                    "failed_confirmed": "failed",
                }.get(latest_attempt.status, "failed")
                if latest_attempt is not None
                else "approved"
            )
            attempt_id = (
                latest_attempt.attempt_id if latest_attempt is not None else None
            )

    unresolved_issue_count = int(
        await db.scalar(
            select(func.count())
            .select_from(MonthlyCloseIssueInstance)
            .where(
                MonthlyCloseIssueInstance.cycle_id == cycle.cycle_id,
                MonthlyCloseIssueInstance.status.notin_(("resolved", "cancelled")),
            )
        )
        or 0
    )
    settlement_count = 0
    settlement_total = "0.00"
    if include_financial_detail:
        settlement_count = int(
            await db.scalar(
                select(func.count())
                .select_from(OwnerSettlement)
                .where(OwnerSettlement.billing_month == cycle.billing_month)
            )
            or 0
        )
        settlement_total = format(
            Decimal(
                await db.scalar(
                    select(
                        func.coalesce(func.sum(OwnerSettlement.actual_owner_amount), 0)
                    ).where(OwnerSettlement.billing_month == cycle.billing_month)
                )
                or 0
            ).quantize(Decimal("0.01")),
            ".2f",
        )
    confirmed_count=paid_count=0
    if include_financial_detail:
        from app.models.settlement import SettlementStatus
        statuses=(await db.execute(select(OwnerSettlement.status,func.count()).where(
            OwnerSettlement.billing_month==cycle.billing_month).group_by(OwnerSettlement.status))).all()
        counts={str(getattr(status,'value',status)):count for status,count in statuses}
        paid_count=counts.get('paid',0)
        confirmed_count=counts.get('confirmed',0)+paid_count
    delivery_rows = ()
    if include_financial_detail:
        from app.models.owner import Owner
        rows = (await db.execute(select(
            OwnerSettlement.settlement_id, OwnerSettlement.billing_month,
            OwnerSettlement.actual_owner_amount, OwnerSettlement.status, Owner.name,
        ).outerjoin(Owner, Owner.owner_id == OwnerSettlement.owner_id).where(
            OwnerSettlement.billing_month == cycle.billing_month,
        ).order_by(OwnerSettlement.settlement_id))).all()
        delivery_rows = tuple({
            "settlement_id": row.settlement_id, "billing_month": row.billing_month,
            "owner_name": row.name or "未登记姓名的业主",
            "amount": format(Decimal(str(row.actual_owner_amount)).quantize(Decimal("0.01")), ".2f"),
            "status": str(getattr(row.status, "value", row.status)),
        } for row in rows)
    status_message=(f"业主结算共 {settlement_count} 份，已确认 {confirmed_count} 份，已付款 {paid_count} 份。"
        + ("整月关账已完成。" if state=='verified' else "整月经营关账仍需核对资料与剩余事项，已确认的业主账单无需重复确认。")) if settlement_count and include_financial_detail else ""
    return FinalReviewProjection(
        confirmed_settlement_count=confirmed_count,paid_settlement_count=paid_count,
        settlement_status_message=status_message,
        settlements=delivery_rows,
        state=state,
        source_complete_count=sum(
            source.state in {"completed", "not_applicable"} for source in sources
        ),
        source_total_count=len(sources),
        unresolved_issue_count=unresolved_issue_count,
        settlement_count=settlement_count,
        settlement_total_amount=settlement_total,
        approval_state=approval_state,
        proposal_id=proposal_id,
        attempt_id=attempt_id,
    )


def _recommend_action(
    cycle: MonthlyCloseCycle,
    sources: tuple[MonthlyCloseSourceProjection, ...],
    *,
    actor_role: str,
    remediation_id: str | None,
    assigned_issue_id: str | None,
    inbox_action_id: str | None,
    inbox_action_source_type: str | None,
    inbox_processing_id: str | None,
    inbox_processing_source_type: str | None,
    workflow_step: dict[str, Any] | None,
    completed_evidence_current: bool,
    finalization_state: str,
) -> RecommendedAction:
    source_by_type = {source.source_type: source for source in sources}

    def safe_target(source_type: str | None) -> tuple[str, str]:
        source = source_by_type.get(source_type or "")
        return (
            ("source", source.source_id)
            if source is not None
            else ("cycle", cycle.cycle_id)
        )

    if cycle.status == "completed":
        if completed_evidence_current and finalization_state == "verified":
            return RecommendedAction(
                kind="none",
                label="本月月结已完成",
                reason_code="monthly_close_completed",
                target_type="cycle",
                target_id=cycle.cycle_id,
            )
        if finalization_state != "verified":
            return RecommendedAction(
                kind="wait",
                label="等待最终确定性复核完成",
                reason_code="finalization_verification_required",
                target_type="cycle",
                target_id=cycle.cycle_id,
            )
        if actor_role == "admin":
            return RecommendedAction(
                kind="reopen_cycle",
                label="填写原因并重新打开月结",
                reason_code="completed_evidence_stale",
                target_type="cycle",
                target_id=cycle.cycle_id,
            )
        return RecommendedAction(
            kind="escalate",
            label="请管理员重新打开已完成月结",
            reason_code="completed_cycle_reopen_required",
            target_type="cycle",
            target_id=cycle.cycle_id,
        )

    if actor_role in {"cleaner", "keeper"}:
        if assigned_issue_id is not None:
            return RecommendedAction(
                kind="escalate",
                label="请联系管理员处理分配事项",
                reason_code="assigned_issue_admin_required",
                target_type="cycle",
                target_id=cycle.cycle_id,
            )
        needs_action = next(
            (
                source
                for source in sources
                if source.state in {"needs_action", "blocked"}
            ),
            None,
        )
        if needs_action is not None:
            return RecommendedAction(
                kind="wait_for_review",
                label="等待管理员复核我提交的资料",
                reason_code="own_upload_review_required",
                target_type="source",
                target_id=needs_action.source_id,
            )
        processing = next(
            (source for source in sources if source.state == "processing"), None
        )
        if processing is not None:
            return RecommendedAction(
                kind="wait",
                label="等待我提交的资料处理完成",
                reason_code="own_source_processing",
                target_type="source",
                target_id=processing.source_id,
            )
        missing = next(
            (source for source in sources if source.state == "missing"), None
        )
        if missing is not None:
            return RecommendedAction(
                kind="upload_own_source",
                label="提交职责范围内的月结资料",
                reason_code="own_source_missing",
                target_type="source",
                target_id=missing.source_id,
            )
        return RecommendedAction(
            kind="wait_for_assignment",
            label="等待新的月结事项分配",
            reason_code="no_assigned_monthly_close_work",
            target_type="cycle",
            target_id=cycle.cycle_id,
        )

    if remediation_id is not None:
        return RecommendedAction(
            kind="view_remediation",
            label="查看执行结果不确定事项",
            reason_code="unresolved_execution_state",
            target_type="remediation",
            target_id=remediation_id,
        )
    blocked = next((source for source in sources if source.state == "blocked"), None)
    if blocked is not None:
        if actor_role != "admin":
            return RecommendedAction(
                kind="escalate",
                label="请管理员处理月结安全阻塞",
                reason_code="admin_confirmation_required",
                target_type="source",
                target_id=blocked.source_id,
            )
        return RecommendedAction(
            kind="view_blocker",
            label="查看安全阻塞并联系管理员",
            reason_code="source_safety_blocked",
            target_type="source",
            target_id=blocked.source_id,
        )
    if inbox_action_id is not None:
        if actor_role != "admin":
            target_type, target_id = safe_target(inbox_action_source_type)
            return RecommendedAction(
                kind="escalate",
                label="请管理员确认收件资料",
                reason_code="admin_confirmation_required",
                target_type=target_type,
                target_id=target_id,
            )
        return RecommendedAction(
            kind="confirm_inbox",
            label="确认收件资料类型",
            reason_code="inbox_confirmation_required",
            target_type="inbox",
            target_id=inbox_action_id,
        )
    work_document = next((document for source in sources for document in source.documents
                          if document.analysis_state == "work_log_ready"), None)
    if work_document is not None and actor_role == "admin":
        return RecommendedAction(
            kind="review_work_log", label="核对保洁记录与续住费用",
            reason_code="work_log_separate_reconciliation", target_type="document",
            target_id=work_document.document_id,
        )
    needs_confirmation = next(
        (
            source
            for source in sources
            if source.state == "needs_action"
            and any(
                document.analysis_state in {"needs_mapping", "failed"}
                for document in source.documents
            )
        ),
        None,
    )
    if needs_confirmation is not None:
        if actor_role != "admin":
            return RecommendedAction(
                kind="escalate",
                label="请管理员确认资料识别结果",
                reason_code="admin_confirmation_required",
                target_type="source",
                target_id=needs_confirmation.source_id,
            )
        return RecommendedAction(
            kind="confirm_analysis",
            label="确认资料识别结果",
            reason_code="analysis_confirmation_required",
            target_type="source",
            target_id=needs_confirmation.source_id,
        )
    processing = next(
        (source for source in sources if source.state == "processing"), None
    )
    if processing is not None:
        return RecommendedAction(
            kind="wait",
            label="等待当前资料处理完成",
            reason_code="source_processing",
            target_type="source",
            target_id=processing.source_id,
        )
    if inbox_processing_id is not None:
        if actor_role != "admin":
            target_type, target_id = safe_target(inbox_processing_source_type)
            return RecommendedAction(
                kind="wait",
                label="等待收件资料处理完成",
                reason_code="inbox_processing",
                target_type=target_type,
                target_id=target_id,
            )
        return RecommendedAction(
            kind="wait",
            label="等待收件资料处理完成",
            reason_code="inbox_processing",
            target_type="inbox",
            target_id=inbox_processing_id,
        )
    missing = next((source for source in sources if source.state == "missing"), None)
    if missing is not None:
        if actor_role != "admin":
            return RecommendedAction(
                kind="escalate",
                label="请管理员补充本月资料",
                reason_code="admin_source_action_required",
                target_type="source",
                target_id=missing.source_id,
            )
        return RecommendedAction(
            kind="provide_source",
            label="补充本月资料",
            reason_code="expected_source_missing",
            target_type="source",
            target_id=missing.source_id,
        )
    analyzable = next(
        (source for source in sources if source.state == "needs_action"), None
    )
    if analyzable is not None:
        if actor_role != "admin":
            return RecommendedAction(
                kind="escalate",
                label="请管理员继续资料分析",
                reason_code="admin_source_action_required",
                target_type="source",
                target_id=analyzable.source_id,
            )
        return RecommendedAction(
            kind="continue_analysis",
            label="继续分析已保存资料",
            reason_code="source_ready_for_analysis",
            target_type="source",
            target_id=analyzable.source_id,
        )
    if workflow_step is not None:
        if actor_role == "admin" and workflow_step["status"] == "ready":
            return RecommendedAction(
                kind="confirm_workflow_step",
                label="确认下一项月结步骤",
                reason_code="workflow_confirmation_required",
                target_type="workflow_step",
                target_id=str(workflow_step["step_key"]),
            )
        return RecommendedAction(
            kind="escalate" if actor_role != "admin" else "view_workflow_blocker",
            label="请管理员处理下一项月结步骤",
            reason_code=(
                "admin_workflow_action_required"
                if actor_role != "admin"
                else "workflow_step_blocked"
            ),
            target_type="cycle" if actor_role != "admin" else "workflow_step",
            target_id=(
                cycle.cycle_id
                if actor_role != "admin"
                else str(workflow_step["step_key"])
            ),
        )
    if actor_role == "admin" and finalization_state in {
        "not_started",
        "pending_approval",
        "approved",
        "failed",
        "stale",
        "reopened",
    }:
        return RecommendedAction(
            kind="start_final_review",
            label="开始最终复核",
            reason_code="final_review_ready",
            target_type="cycle",
            target_id=cycle.cycle_id,
        )
    return RecommendedAction(
        kind="escalate",
        label="请管理员核对月结最终状态",
        reason_code="workflow_finalization_inconsistent",
        target_type="cycle",
        target_id=cycle.cycle_id,
    )


def _input_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()


async def build_monthly_close_projection(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    actor: User | dict[str, Any],
) -> MonthlyCloseProjection:
    actor_id, actor_role = _actor_identity(actor)
    features = {
        "assistant_enabled": bool(settings.MONTHLY_CLOSE_ASSISTANT_ENABLED)
        and (not settings.MONTHLY_CLOSE_ASSISTANT_ADMIN_ONLY or actor_role == "admin"),
        "external_intake_enabled": bool(settings.MONTHLY_CLOSE_EXTERNAL_INTAKE_ENABLED),
        "source_adapters": bool(settings.MONTHLY_CLOSE_SOURCE_ADAPTERS_ENABLED)
        and (not settings.MONTHLY_CLOSE_ASSISTANT_ADMIN_ONLY or actor_role == "admin"),
    }
    current_actor = await db.scalar(
        select(User)
        .where(User.user_id == actor_id)
        .execution_options(populate_existing=True)
    )
    can_advance_final_review = bool(
        current_actor is not None
        and current_actor.is_active
        and current_actor.role == UserRole.admin
    )
    financial_case = None
    if actor_role == "admin":
        from app.services.monthly_close.financial_case_bridge import build_financial_case_snapshot
        financial_case = await build_financial_case_snapshot(db, cycle)
    source_types = ROLE_SOURCE_TYPES[actor_role]
    document_source_types = ROLE_DOCUMENT_SOURCE_TYPES[actor_role]
    requirements = list(
        (
            await db.execute(
                select(
                    MonthlyCloseSourceRequirement.requirement_id,
                    MonthlyCloseSourceRequirement.source_type,
                    MonthlyCloseSourceRequirement.state,
                    MonthlyCloseSourceRequirement.not_applicable_reason,
                ).where(
                    MonthlyCloseSourceRequirement.cycle_id == cycle.cycle_id,
                    MonthlyCloseSourceRequirement.source_type.in_(source_types),
                )
            )
        ).mappings()
    )
    requirement_by_source = {row["source_type"]: row for row in requirements}
    rows = await _load_document_rows(
        db,
        cycle.cycle_id,
        actor_id=actor_id,
        actor_role=actor_role,
        source_types=document_source_types,
    )
    document_ids = [str(row["document_id"]) for row in rows]
    analyses = await _load_latest_analyses(
        db,
        cycle_id=cycle.cycle_id,
        document_ids=document_ids,
        include_financial_detail=False,
    )
    current_classification_generations = await _load_current_classification_generations(
        db,
        cycle_id=cycle.cycle_id,
        document_ids=document_ids,
    )
    active_jobs = await _load_active_analysis_jobs(
        db, cycle_id=cycle.cycle_id, document_ids=document_ids
    )
    documents_by_source: dict[str, list[ProjectedDocument]] = {}
    for row in rows:
        document_id = str(row["document_id"])
        analysis = analyses.get(document_id)
        current_generation = current_classification_generations.get(document_id, 1)
        if analysis is not None and (
            analysis["source_type"] != row["source_type"]
            or int(analysis["classification_generation"]) != current_generation
        ):
            analysis = None
        logical_source_type = (
            "utility_expense"
            if row["source_type"] == "utility_receipt"
            else str(row["source_type"])
        )
        documents_by_source.setdefault(logical_source_type, []).append(
            _document_projection(
                row,
                analysis,
                job_active=document_id in active_jobs,
                has_confirmed_receipt=document_id in current_classification_generations,
                actor_role=actor_role,
                projected_source_type=logical_source_type,
            )
        )
    supplier_residual_sources: set[str] = set()
    if document_ids:
        supplier_residual_sources = set(
            await db.scalars(
                select(MonthlyCloseDocument.source_type)
                .join(
                    MonthlyCloseServiceLine,
                    MonthlyCloseServiceLine.document_id
                    == MonthlyCloseDocument.document_id,
                )
                .where(
                    MonthlyCloseDocument.document_id.in_(document_ids),
                    MonthlyCloseDocument.source_type.in_(
                        ("cleaning_statement", "linen_statement")
                    ),
                    MonthlyCloseServiceLine.match_status != "matched",
                )
                .distinct()
            )
        )

    live_classify_job = exists(
        select(MonthlyCloseProcessingJob.job_id).where(
            MonthlyCloseProcessingJob.cycle_id == cycle.cycle_id,
            MonthlyCloseProcessingJob.job_type == "classify_document",
            MonthlyCloseProcessingJob.subject_type == "monthly_close_inbox",
            MonthlyCloseProcessingJob.subject_id == MonthlyCloseInboxItem.item_id,
            MonthlyCloseProcessingJob.status.in_(("pending", "leased")),
        )
    ).label("has_live_classify_job")
    inbox_columns = [
        MonthlyCloseInboxItem.source_type,
        MonthlyCloseInboxItem.status,
        MonthlyCloseInboxItem.classification_generation,
        MonthlyCloseInboxItem.updated_at,
        live_classify_job,
    ]
    if actor_role == "admin" or actor_role in {"cleaner", "keeper"}:
        inbox_columns.insert(0, MonthlyCloseInboxItem.item_id)
    inbox_statement = select(*inbox_columns).where(
        MonthlyCloseInboxItem.cycle_id == cycle.cycle_id,
        MonthlyCloseInboxItem.status.notin_(("confirmed", "dismissed")),
    )
    if actor_role in {"cleaner", "keeper"}:
        inbox_statement = inbox_statement.where(
            MonthlyCloseInboxItem.created_by == actor_id,
            MonthlyCloseInboxItem.source_type.in_(source_types),
        )
    inbox_rows = list(
        (
            await db.execute(
                inbox_statement.order_by(
                    MonthlyCloseInboxItem.updated_at,
                    MonthlyCloseInboxItem.item_id,
                )
            )
        ).mappings()
    )
    inbox_by_source: dict[str, list[tuple[str, bool]]] = {}
    for row in inbox_rows:
        if row["source_type"] is not None:
            logical_source_type = (
                "utility_expense"
                if row["source_type"] == "utility_receipt"
                else str(row["source_type"])
            )
            inbox_by_source.setdefault(logical_source_type, []).append(
                (str(row["status"]), bool(row["has_live_classify_job"]))
            )

    # Original-file accounting can finish without the legacy adapter processing
    # its archived copy. Only live expense matches may supersede that marker.
    if financial_case is not None:
        completed_documents = {source["document_id"] for source in financial_case["sources"]
                               if source["document_id"] and source["expense_complete"]
                               and source["kind"] == "utility"}
        for source_type, documents in documents_by_source.items():
            documents_by_source[source_type] = [
                replace(document, analysis_state="ready", analysis_kind="financial_case",
                        analysis_message="原件费用已核对，当前账本已匹配。")
                if document.document_id in completed_documents else document for document in documents
            ]
    sources: list[MonthlyCloseSourceProjection] = []
    for source_type in source_types:
        requirement = requirement_by_source.get(source_type)
        if requirement is None:
            continue
        documents = tuple(
            sorted(
                documents_by_source.get(source_type, []),
                key=lambda document: document.document_id,
            )
        )
        state = _source_state(
            requirement, documents, inbox_facts=tuple(inbox_by_source.get(source_type, [])),
            can_observe_global_requirement=actor_role in {"admin", "finance", "operator"},
            supplier_residual=source_type in supplier_residual_sources,
        )
        originals = [source for source in (financial_case or {}).get("sources", [])
                     if source_type in source["source_types"]]
        if originals and state in {"missing", "blocked"}:
            state = "completed" if all(source["expense_complete"] for source in originals) else "needs_action"
        if originals and any(source["pending_count"] for source in originals) and state == "completed":
            state = "needs_action"
        sources.append(
            MonthlyCloseSourceProjection(
                source_id=str(requirement["requirement_id"]),
                source_type=source_type,
                state=state,
                documents=documents,
                not_applicable_reason=requirement["not_applicable_reason"],
            )
        )
    source_tuple = tuple(sources)
    workflow_evidence: list[dict[str, Any]] = []
    effective_workflow_steps: list[dict[str, Any]] = []
    evidence_rows = None
    if actor_role in FINANCIAL_DETAIL_ROLES:
        evidence_rows = await build_step_evidences(db, cycle, financial_case=financial_case)
        effective_workflow_steps = await build_effective_steps(db, cycle, evidence_rows)
        workflow_evidence = [
            _workflow_evidence_projection(
                evidence,
                effective,
                actor_role=actor_role,
            )
            for evidence, effective in zip(
                evidence_rows, effective_workflow_steps, strict=True
            )
        ]
    remediation_id = None
    if actor_role in FINANCIAL_DETAIL_ROLES and cycle.status != "completed":
        remediation_id = await db.scalar(
            select(MonthlyCloseRemediation.remediation_id)
            .where(
                MonthlyCloseRemediation.cycle_id == cycle.cycle_id,
                MonthlyCloseRemediation.status != "resolved",
            )
            .order_by(
                MonthlyCloseRemediation.created_at,
                MonthlyCloseRemediation.remediation_id,
            )
            .limit(1)
        )
    assigned_issue_id = None
    if actor_role in {"cleaner", "keeper"} and cycle.status != "completed":
        assigned_issue_id = await db.scalar(
            select(MonthlyCloseIssueInstance.issue_id)
            .where(
                MonthlyCloseIssueInstance.cycle_id == cycle.cycle_id,
                MonthlyCloseIssueInstance.assigned_to == actor_id,
                MonthlyCloseIssueInstance.status.notin_(("resolved", "cancelled")),
            )
            .order_by(
                MonthlyCloseIssueInstance.created_at,
                MonthlyCloseIssueInstance.issue_id,
            )
            .limit(1)
        )
    workflow_step = next(
        (step for step in effective_workflow_steps if step["status"] != "confirmed"),
        None,
    )
    completed_evidence_current = (
        await completed_cycle_is_fresh(
            db,
            cycle,
            evidences=evidence_rows,
        )
        if cycle.status == "completed"
        else False
    )
    final_close_blockers = [
        source.source_id
        for source in source_tuple
        if source.state in {"missing", "processing", "needs_action", "blocked"}
    ]
    if remediation_id is not None:
        final_close_blockers.append(remediation_id)
    if assigned_issue_id is not None:
        final_close_blockers.append(str(assigned_issue_id))
    if actor_role == "admin":
        final_close_blockers.extend(str(row["item_id"]) for row in inbox_rows)
    if workflow_step is not None:
        final_close_blockers.append(f"workflow_step:{workflow_step['step_key']}")
    elif actor_role not in FINANCIAL_DETAIL_ROLES and cycle.status != "completed":
        # Roles without financial workflow evidence receive a generic blocker,
        # never an empty list that could imply global final-close readiness.
        final_close_blockers.append("workflow:admin_review_required")
    if cycle.status == "completed":
        final_close_blockers = (
            [] if completed_evidence_current else ["cycle:reopen_required"]
        )
    inbox_action_row = next(
        (
            row
            for row in inbox_rows
            if row["status"] in {"needs_review", "failed", "classified"}
            or (row["status"] == "received" and not bool(row["has_live_classify_job"]))
        ),
        None,
    )
    inbox_processing_row = next(
        (
            row
            for row in inbox_rows
            if row["status"] == "received" and bool(row["has_live_classify_job"])
        ),
        None,
    )
    final_review = await _final_review_projection(
        db,
        cycle,
        source_tuple,
        include_financial_detail=actor_role in FINANCIAL_DETAIL_ROLES,
    )
    if financial_case is not None:
        final_review = replace(
            final_review,
            unresolved_issue_count=final_review.unresolved_issue_count + len(financial_case["pending_issues"]),
        )
    recommended_action = _recommend_action(
        cycle,
        source_tuple,
        actor_role=actor_role,
        remediation_id=remediation_id,
        assigned_issue_id=(str(assigned_issue_id) if assigned_issue_id else None),
        inbox_action_id=(
            str(inbox_action_row["item_id"])
            if inbox_action_row is not None and inbox_action_row.get("item_id")
            else cycle.cycle_id
        )
        if inbox_action_row is not None
        else None,
        inbox_action_source_type=(
            str(inbox_action_row["source_type"])
            if inbox_action_row is not None
            and inbox_action_row["source_type"] is not None
            else None
        ),
        inbox_processing_id=(
            str(inbox_processing_row["item_id"])
            if inbox_processing_row is not None and inbox_processing_row.get("item_id")
            else cycle.cycle_id
        )
        if inbox_processing_row is not None
        else None,
        inbox_processing_source_type=(
            str(inbox_processing_row["source_type"])
            if inbox_processing_row is not None
            and inbox_processing_row["source_type"] is not None
            else None
        ),
        workflow_step=workflow_step,
        completed_evidence_current=completed_evidence_current,
        finalization_state=final_review.state,
    )
    hash_material = {
        "financial_case": financial_case,
        "projection_version": PROJECTION_VERSION,
        "cycle_id": cycle.cycle_id,
        "billing_month": cycle.billing_month,
        "cycle_status": cycle.status,
        "actor_id": actor_id,
        "actor_role": actor_role,
        "features": features,
        "can_advance_final_review": can_advance_final_review,
        "sources": [source.to_dict() for source in source_tuple],
        "workflow_evidence_hashes": [
            item.get("evidence_hash") for item in workflow_evidence
        ],
        "pending_inbox": [
            {
                **(
                    {"item_id": row["item_id"]}
                    if row.get("item_id") is not None
                    else {}
                ),
                "source_type": row["source_type"],
                "status": row["status"],
                "classification_generation": row["classification_generation"],
                "has_live_classify_job": bool(row["has_live_classify_job"]),
                "updated_at": _iso(row["updated_at"]),
            }
            for row in inbox_rows
        ],
        "final_close_blockers": final_close_blockers,
        "final_review": asdict(final_review),
        "recommended_action": asdict(recommended_action),
    }
    return MonthlyCloseProjection(
        projection_version=PROJECTION_VERSION,
        computed_at=datetime.now(timezone.utc).isoformat(),
        input_hash=_input_hash(hash_material),
        cycle_id=cycle.cycle_id,
        billing_month=cycle.billing_month,
        cycle_status=cycle.status,
        actor_role=actor_role,
        features=features,
        can_advance_final_review=can_advance_final_review,
        sources=source_tuple,
        workflow_evidence=workflow_evidence,
        final_close_blockers=final_close_blockers,
        final_review=final_review,
        recommended_action=recommended_action,
        financial_case=financial_case,
    )


async def get_visible_projection_document(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    actor: User | dict[str, Any],
    document_id: str,
) -> ProjectedDocument | None:
    """Return one cycle- and actor-scoped document without loading its blob."""
    actor_id, actor_role = _actor_identity(actor)
    rows = await _load_document_rows(
        db,
        cycle.cycle_id,
        actor_id=actor_id,
        actor_role=actor_role,
        source_types=ROLE_DOCUMENT_SOURCE_TYPES[actor_role],
        document_id=document_id,
        include_detail=True,
    )
    if not rows:
        return None
    row = rows[0]
    analyses = await _load_latest_analyses(
        db,
        cycle_id=cycle.cycle_id,
        document_ids=[document_id],
        include_financial_detail=actor_role in FINANCIAL_DETAIL_ROLES,
    )
    current_generations = await _load_current_classification_generations(
        db,
        cycle_id=cycle.cycle_id,
        document_ids=[document_id],
    )
    active_jobs = await _load_active_analysis_jobs(
        db, cycle_id=cycle.cycle_id, document_ids=[document_id]
    )
    analysis = analyses.get(document_id)
    if analysis is not None and (
        analysis["source_type"] != row["source_type"]
        or int(analysis["classification_generation"])
        != current_generations.get(document_id, 1)
    ):
        analysis = None
    document = _document_projection(
        row,
        analysis,
        job_active=document_id in active_jobs,
        has_confirmed_receipt=document_id in current_generations,
        actor_role=actor_role,
    )
    if actor_role == "admin":
        from app.services.monthly_close.financial_case_bridge import build_financial_case_snapshot
        financial_case = await build_financial_case_snapshot(db, cycle)
        if any(source["document_id"] == document_id and source["expense_complete"]
               and source["kind"] == "utility" for source in financial_case["sources"]):
            document = replace(document, analysis_state="ready", analysis_kind="financial_case",
                               analysis_message="原件费用已核对，当前账本已匹配。")
    return document
