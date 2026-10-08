"""Administrator-only API for the fixed nine-step monthly close."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
import logging
from functools import wraps
from typing import Literal
from urllib.parse import quote
from uuid import uuid4

import openai
from fastapi import APIRouter, Depends, File, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import Response, StreamingResponse
from fastapi.routing import APIRoute
from starlette.datastructures import Headers
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.deps import (
    RedisClient,
    get_current_user,
    get_db,
    require_monthly_close_action,
)
from app.core.config import settings
from app.core.rate_limit import enforce_rate_limit
from app.models.monthly_close import MonthlyCloseCycle, MonthlyCloseInboxItem
from app.models.order import Order
from app.models.monthly_close_control import (
    MonthlyCloseApproval,
    MonthlyCloseDocumentAnalysis,
    MonthlyCloseExecutionAttempt,
    MonthlyCloseProcessingJob,
    MonthlyCloseProposal,
    MonthlyCloseVerification,
)
from app.models.room import Room
from app.models.user import User
from app.models.recon import ReconBatch, ReconDiff
from app.services.monthly_close.workflow import (
    MonthlyCloseConflict,
    build_lightweight_cycle_summaries,
    build_cycle_summary,
    build_cycle_view,
    build_role_cycle_projection,
    confirm_step,
    get_cycle_by_month,
    get_or_create_cycle,
    require_cycle_writable,
    reopen_cycle,
)
from app.services.monthly_close.documents import (
    MAX_MONTHLY_CLOSE_DOCUMENT_BYTES,
    MonthlyCloseDocumentError,
    archive_document,
    get_document,
    mark_source_not_applicable,
    normalize_current_source_type,
    store_document,
    validate_document,
)
from app.services.monthly_close.engine_actions import (
    reconcile_service_fees_from_documents,
    run_utility_from_documents,
)
from app.services.monthly_close.operating_expenses import (
    build_room_aliases,
    import_operating_expense_document,
    import_operating_expense_documents,
)
from app.services.monthly_close.operating_expense_mapping import (
    OperatingExpenseAnalysis,
    OperatingExpenseMappingCoordinates,
    OperatingExpenseMappingError,
    OperatingExpenseReplacementContext,
    analyze_operating_expense_statement,
)
from app.services.monthly_close.operating_expense_lineage import (
    confirmed_operating_event_metadata,
    operating_replacement_context,
)
from app.services.utility_recon.ai_mapping import UtilityColumnMapping
from app.services.utility_recon.contracts import WorkbookInput
from app.services.utility_recon.workbook import (
    WorkbookInspectionError,
    inspect_workbook_with_ai,
    summarize_inspected_file,
)
from app.api.v1.billing_recon import analyze_bill, confirm_bill
from app.models.monthly_close import (
    MonthlyCloseDocument,
    uses_legacy_utility_contract,
)
from app.services.audit import log_action_tx
from app.services.monthly_close.service_mapping import (
    ServiceMappingCoordinates,
    ServiceMappingError,
    ServiceStatementAnalysis,
    analyze_service_statement,
)
from app.services.monthly_close.service_reconciliation import process_service_document
from app.services.monthly_close.service_statement import ServiceStatementError
from app.services.monthly_close.layout_memory import (
    find_remembered_mapping,
    layout_memory_metrics,
    list_layout_memories,
    remember_document_mapping,
    set_layout_memory_enabled,
)
from app.services.monthly_close.source_classifier import (
    SourceClassification,
    classify_monthly_close_source,
)
from app.services.monthly_close.spreadsheet_preview import build_workbook_preview
from app.services.monthly_close.inbox import (
    MonthlyCloseInboxError,
    classify_inbox_item,
    confirm_inbox_item,
    create_intake_link,
    inbox_item_view,
    intake_link_view,
    list_inbox_items,
    list_intake_links,
    permanently_delete_inbox_item,
    receive_inbox_item,
    resolve_intake_link,
    revoke_intake_link,
    set_inbox_source,
)
from app.services.monthly_close.processing import (
    complete_document_analysis_manually,
    durable_receipt_view,
    receive_monthly_close_file,
)
from app.services.monthly_close.projection import ROLE_SOURCE_TYPES, get_visible_projection_document
from app.services.monthly_close.assistant import (
    AssistantAttachmentNotFound,
    AssistantAuthorizationChanged,
    answer_monthly_close_message,
    list_monthly_close_chat_history,
)
from app.services.monthly_close.events import (
    EventCursorExpired,
    EventCursorInvalid,
    EventCursorRefreshRequired,
    cycle_event_high_watermark,
    list_cycle_events,
    lock_cycle_for_snapshot,
    public_cycle_cursor,
)
from app.services.monthly_close.control import (
    MonthlyCloseControlError,
    approve_proposal,
    create_proposal,
    execute_approved_proposal,
    reject_proposal,
)
from app.services.monthly_close.adapters import (
    adapt_operating_expense_source,
    adapt_service_source,
    adapt_utility_source,
    build_operating_expense_proposal,
    build_ota_appeal_adjudication_proposal,
    build_ota_proposal,
    build_service_proposal,
    build_utility_proposal,
    source_contract_versions,
    list_ota_appeal_settlement_candidates,
    reconcile_settled_ota_issue,
    stage_ota_appeal_settlement_candidates,
)
from app.services.monthly_close.source_contract import source_evidence_is_current
from app.services.monthly_close.evidence import build_step_evidences
from app.services.monthly_close.finalization import build_finalization_proposal
from app.services.monthly_close.verification import verify_attempt
from app.services.monthly_close.migration import (
    MonthlyCloseRolloutError,
    legacy_history_view,
    migrate_legacy_monthly_close_history,
    require_cycle_write_control,
    switch_write_control,
)
from app.services.monthly_close.monitor import monthly_close_rollout_health
from app.services.monthly_close.permissions import (
    MonthlyCloseAction,
    MonthlyCloseFeature,
    MonthlyClosePermissionContext,
    MonthlyClosePermissionDenied,
    assert_monthly_close_feature_enabled,
    authorize_current_monthly_close_actor,
)


logger = logging.getLogger(__name__)


async def _wake_monthly_close_task(db, cycle_id):
    """Do not turn a committed finance response into a background queue error."""
    from app.services.monthly_close.task_runtime import mark_dirty
    try:
        await mark_dirty(db, cycle_id)
    except Exception:
        await db.rollback()
        # Raw exceptions can contain financial source material or credentials.
        logger.warning("monthly_close_task_wake_failed")


def _task_evidence_mutation(path, methods):
    if not set(methods or []) & {"POST", "PATCH", "DELETE"} or "{billing_month}" not in path:
        return False
    suffix = path.split("{billing_month}", 1)[1]
    if suffix in {"/task", "/messages"} or "/intake-links" in suffix:
        return False
    # Rendering/downloads and previews do not change financial evidence.
    if any(part in {"preview", "download", "export", "artifacts", "package"} for part in suffix.split("/")):
        return False
    return True


class MonthlyCloseTaskWakeRoute(APIRoute):
    """Wake on successful mutations, after their own transaction has committed."""
    def __init__(self, path, endpoint, **kwargs):
        if _task_evidence_mutation(path, kwargs.get("methods")) and not getattr(endpoint, "_monthly_close_task_wake", False):
            original_endpoint = endpoint
            @wraps(endpoint)
            async def with_task_wakeup(*args, **values):
                result = await original_endpoint(*args, **values)
                if isinstance(result, Response) and result.status_code >= 400:
                    return result
                db = values.get("db")
                month = values.get("billing_month")
                if db is not None and month:
                    try:
                        cycle = await get_cycle_by_month(db, month)
                        if cycle is not None:
                            await _wake_monthly_close_task(db, cycle.cycle_id)
                    except Exception:
                        await db.rollback()
                        logger.warning("monthly_close_task_wake_failed")
                return result
            with_task_wakeup._monthly_close_task_wake = True
            endpoint = with_task_wakeup
        super().__init__(path, endpoint, **kwargs)


router = APIRouter(prefix="/monthly-close", tags=["monthly-close"], route_class=MonthlyCloseTaskWakeRoute)


class MonthlyCloseTaskBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    action: Literal["start", "resume", "pause"]
    goal: str | None = Field(default=None, min_length=1, max_length=4000)
    expected_revision: int | None = Field(default=None, ge=1)



class ConfirmStepBody(BaseModel):
    expected_evidence_hash: str = Field(min_length=64, max_length=64)
    note: str | None = Field(default=None, max_length=1000)


class ReopenBody(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


class ProposalCreateBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=80)
    proposal_type: Literal[
        "ota_reconciliation",
        "ota_appeal_adjudication",
        "service_fee_reconciliation",
        "utility_reconciliation",
        "operating_expense_import",
        "generate_owner_settlements",
        "issue_resolution",
        "finalize_monthly_close",
    ]
    evidence_hash: str = Field(min_length=64, max_length=64)
    commands: list[dict]


class ProposalDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=80)
    reason: str | None = Field(default=None, max_length=1000)


class ProposalExecuteBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=80)


class FinalizationProposalBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=80)


class SourceProposalBody(BaseModel):
    """Request metadata only; source commands are always built server-side."""

    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=80)


class OtaDecisionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    diff_id: str = Field(min_length=1, max_length=40)
    decision: Literal["action", "claim", "dismiss"]
    action: str | None = Field(default=None, max_length=32)
    order_id: str | None = Field(default=None, max_length=20)
    reason: str | None = Field(default=None, max_length=1000)


class OtaProposalBody(BaseModel):
    """Selection-only input; canonical write commands are built from locked rows."""

    model_config = ConfigDict(extra="forbid")

    batch_id: str = Field(min_length=1, max_length=40)
    selected_diff_ids: list[str] = Field(default_factory=list, max_length=500)
    decisions: list[OtaDecisionBody] = Field(default_factory=list, max_length=500)
    request_id: str = Field(min_length=1, max_length=80)

    @model_validator(mode="after")
    def selection_or_decision_required(self):
        if not self.selected_diff_ids and not self.decisions:
            raise ValueError("at least one OTA selection or decision is required")
        return self


class VerificationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: str = Field(min_length=1, max_length=80)


class OtaAppealReconcileBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    issue_id: str = Field(min_length=1, max_length=24)
    request_id: str = Field(min_length=1, max_length=80)


class OtaAppealAdjudicationBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1, max_length=24)
    action: Literal["withdraw", "confirm_identity"]
    identity_choice_id: str | None = Field(default=None, max_length=24)
    reason: str = Field(min_length=1, max_length=1000)
    request_id: str = Field(min_length=1, max_length=80)


class NotApplicableBody(BaseModel):
    reason: str = Field(max_length=1000)


class OtaAnalyzeBody(BaseModel):
    coordinates: dict | None = None


class OtaConfirmBody(BaseModel):
    coordinates: dict
    platform_scope: str
    remember_layout: bool = True


class ServiceAnalyzeBody(BaseModel):
    mapping: ServiceMappingCoordinates | None = None


class ServiceConfirmBody(BaseModel):
    mapping: ServiceMappingCoordinates


class OperatingExpenseAnalyzeBody(BaseModel):
    mapping: OperatingExpenseMappingCoordinates | None = None


class OperatingExpenseConfirmBody(BaseModel):
    mapping: OperatingExpenseMappingCoordinates


class InboxSourceBody(BaseModel):
    source_type: str


class IntakeLinkCreateBody(BaseModel):
    label: str = Field(min_length=1, max_length=120)
    source_type: str


class LayoutMemoryEnabledBody(BaseModel):
    enabled: bool


class UtilityAnalyzeBody(BaseModel):
    mapping: UtilityColumnMapping | None = None


class UtilityConfirmBody(BaseModel):
    mapping: UtilityColumnMapping


class UtilityAnalysis(BaseModel):
    mapping: UtilityColumnMapping
    months: list[str]
    record_count: int
    total_amount: str
    suggested_by: Literal["deterministic", "ai", "remembered", "administrator"]
    needs_confirmation: bool = True
    sheets: list[dict] = Field(default_factory=list)


class MonthlyCloseMessageBody(BaseModel):
    # Extra client authority/tool fields are intentionally ignored. The actor,
    # role, tool manifest, cycle and attachment scope all come from the server.
    model_config = ConfigDict(extra="ignore")

    text: str = Field(min_length=1, max_length=4000)
    attachment_ids: list[str] = Field(default_factory=list, max_length=20)
    context_run_id: str | None = Field(default=None, pattern=r"^MCR-[A-Z0-9]{20}$")

    @field_validator("text")
    @classmethod
    def reject_blank_text(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("message text must not be blank")
        return value


class WriteControlSwitchBody(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_version: int = Field(ge=1)
    owner: Literal["legacy", "assistant"]


def _http_conflict(exc: MonthlyCloseConflict) -> HTTPException:
    status_code = 422 if exc.code == "invalid_billing_month" else 409
    return HTTPException(status_code=status_code, detail=exc.to_detail())


def _document_error(exc: MonthlyCloseDocumentError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.to_detail())


def _inbox_error(exc: MonthlyCloseInboxError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.to_detail())


SOURCE_LABELS = {
    "cleaning_statement": "保洁打扫记录",
    "linen_statement": "布草／洗涤记录",
    "utility_receipt": "历史水电资料",
    "utility_expense": "水电支出",
    "ota_statement": "OTA平台账单",
    "operating_expenses": "其他运营支出",
}


def _service_statement_error(exc: ServiceStatementError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"code": "service_statement_invalid", "message": str(exc)},
    )


async def _cycle_document(
    db: AsyncSession,
    billing_month: str,
    document_id: str,
    *,
    source_type: str | None = None,
) -> tuple[MonthlyCloseCycle, MonthlyCloseDocument]:
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
    document = await get_document(db, document_id)
    if (
        document is None
        or document.cycle_id != cycle.cycle_id
        or not document.is_active
        or (source_type is not None and document.source_type != source_type)
    ):
        raise HTTPException(404, detail={"code": "document_not_found", "message": "有效文件不存在"})
    return cycle, document


async def _cycle_uses_legacy_utility_contract(
    db: AsyncSession, cycle: MonthlyCloseCycle
) -> bool:
    documents = list(
        await db.scalars(
            select(MonthlyCloseDocument).where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.is_active.is_(True),
                MonthlyCloseDocument.source_type.in_(
                    ("utility_receipt", "utility_expense")
                ),
            )
        )
    )
    return uses_legacy_utility_contract(cycle, documents=documents)


def _upload_from_document(document: MonthlyCloseDocument) -> UploadFile:
    return UploadFile(
        BytesIO(document.content),
        size=document.byte_size,
        filename=document.filename,
        headers=Headers({"content-type": document.mime_type}),
    )


@router.get("")
async def list_monthly_closes(
    current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    cycles = list(
        (
            await db.execute(
                select(MonthlyCloseCycle).order_by(
                    MonthlyCloseCycle.billing_month.desc()
                )
            )
        ).scalars()
    )
    return [await build_cycle_view(db, cycle) for cycle in cycles]


@router.get("/overview")
async def monthly_close_overview(
    current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    cycles = list(
        (
            await db.execute(
                select(MonthlyCloseCycle)
                .order_by(MonthlyCloseCycle.billing_month.desc())
            )
        ).scalars()
    )
    lightweight = await build_lightweight_cycle_summaries(db, cycles)
    result = []
    for cycle in cycles:
        if cycle.cycle_id in lightweight:
            result.append(lightweight[cycle.cycle_id])
        else:
            result.append(await build_cycle_summary(db, cycle))
    return result


@router.get("/layout-memories")
async def get_monthly_close_layout_memories(
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    return await list_layout_memories(db)


@router.get("/layout-memory-metrics")
async def get_monthly_close_layout_memory_metrics(
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    return await layout_memory_metrics(db)


@router.patch("/layout-memories/{document_id}")
async def update_monthly_close_layout_memory(
    document_id: str,
    body: LayoutMemoryEnabledBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        return await set_layout_memory_enabled(
            db,
            document_id,
            enabled=body.enabled,
            user_id=current["user_id"],
        )
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc


@router.post("/{billing_month}")
async def start_monthly_close(
    billing_month: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.finalize_cycle)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_or_create_cycle(db, billing_month, current["user_id"])
        return await build_cycle_view(db, cycle)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.get("/rollout/health")
async def get_monthly_close_rollout_health(
    _current=Depends(
        require_monthly_close_action(MonthlyCloseAction.finalize_cycle)
    ),
    db: AsyncSession = Depends(get_db),
):
    return await monthly_close_rollout_health(db)


@router.post("/{billing_month}/write-control")
async def switch_monthly_close_write_control(
    billing_month: str,
    body: WriteControlSwitchBody,
    current=Depends(
        require_monthly_close_action(MonthlyCloseAction.finalize_cycle)
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise MonthlyCloseRolloutError("cycle_not_found", "月结周期不存在")
        result = await switch_write_control(
            db,
            cycle.cycle_id,
            body.expected_version,
            body.owner,
            current,
        )
        return result.to_dict()
    except MonthlyCloseRolloutError as exc:
        raise _rollout_http_error(exc) from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post("/{billing_month}/legacy-history/project")
async def project_monthly_close_legacy_history(
    billing_month: str,
    _current=Depends(
        require_monthly_close_action(MonthlyCloseAction.finalize_cycle)
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise MonthlyCloseRolloutError("cycle_not_found", "月结周期不存在")
        result = await migrate_legacy_monthly_close_history(db, cycle.cycle_id)
        return {
            "cycle_id": result.cycle_id,
            "created_events": result.created_events,
            "existing_events": result.existing_events,
        }
    except MonthlyCloseRolloutError as exc:
        raise _rollout_http_error(exc) from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.get("/{billing_month}/legacy-history")
async def get_monthly_close_legacy_history(
    billing_month: str,
    _current=Depends(
        require_monthly_close_action(MonthlyCloseAction.approve_proposal)
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise MonthlyCloseRolloutError("cycle_not_found", "月结周期不存在")
        return await legacy_history_view(db, cycle.cycle_id)
    except MonthlyCloseRolloutError as exc:
        raise _rollout_http_error(exc) from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


def _proposal_execution_enabled() -> None:
    _feature_enabled(MonthlyCloseFeature.proposal_execution)


def _feature_enabled(feature: MonthlyCloseFeature, current: dict | None = None) -> None:
    try:
        assert_monthly_close_feature_enabled(feature, current)
    except MonthlyClosePermissionDenied as exc:
        raise HTTPException(
            503,
            detail={"code": exc.code, "message": exc.message},
        ) from exc


async def _reauthorize_monthly_close_action(
    db: AsyncSession,
    current: dict,
    action: MonthlyCloseAction,
    *,
    cycle: MonthlyCloseCycle,
    source_type: str | None = None,
    subject_type: str | None = None,
    subject_id: str | None = None,
    uploaded_by: str | None = None,
) -> None:
    """Recheck current DB authority immediately before a hidden write path."""
    try:
        await authorize_current_monthly_close_actor(
            db,
            current,
            action,
            MonthlyClosePermissionContext(
                cycle_id=cycle.cycle_id,
                subject_cycle_id=cycle.cycle_id,
                source_type=source_type,
                subject_type=subject_type,
                subject_id=subject_id,
                uploaded_by=uploaded_by,
            ),
        )
    except MonthlyClosePermissionDenied as exc:
        raise HTTPException(403, detail="权限不足") from exc


def _control_http_error(exc: MonthlyCloseControlError) -> HTTPException:
    status_code = 422 if exc.code.endswith("_invalid") else 409
    if exc.code.endswith("_forbidden"):
        status_code = 403
    return HTTPException(status_code=status_code, detail=exc.to_detail())


def _rollout_http_error(exc: MonthlyCloseRolloutError) -> HTTPException:
    if exc.code == "cycle_not_found":
        status_code = 404
    elif exc.code == "write_control_actor_forbidden":
        status_code = 403
    elif exc.code == "assistant_cutover_not_ready":
        status_code = 503
    elif exc.code.endswith("_invalid"):
        status_code = 422
    else:
        status_code = 409
    return HTTPException(status_code=status_code, detail=exc.to_detail())


async def _proposal_in_month(
    db: AsyncSession, billing_month: str, proposal_id: str
) -> tuple[MonthlyCloseCycle, MonthlyCloseProposal]:
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(
            404,
            detail={
                "code": "monthly_close_not_found",
                "message": "该月尚未开始月结",
            },
        )
    proposal = await db.scalar(
        select(MonthlyCloseProposal).where(
            MonthlyCloseProposal.proposal_id == proposal_id,
            MonthlyCloseProposal.cycle_id == cycle.cycle_id,
        )
    )
    if proposal is None:
        raise HTTPException(
            404,
            detail={"code": "proposal_not_found", "message": "方案不存在"},
        )
    return cycle, proposal


def _proposal_view(proposal: MonthlyCloseProposal) -> dict:
    return {
        "proposal_id": proposal.proposal_id,
        "cycle_id": proposal.cycle_id,
        "proposal_type": proposal.proposal_type,
        "status": proposal.status,
        "proposal_version": proposal.proposal_version,
        "supersedes_proposal_id": proposal.supersedes_proposal_id,
        "submission_id": (proposal.validation_snapshot or {}).get(
            "create_request_id"
        ),
        "schema_version": proposal.schema_version,
        "canonical_payload": proposal.canonical_payload,
        "payload_hash": proposal.payload_hash,
        "evidence_refs": proposal.evidence_refs,
        "evidence_hash": proposal.evidence_hash,
        "impact_snapshot": proposal.impact_snapshot,
        "approval_policy_snapshot": proposal.approval_policy_snapshot,
        "expires_at": proposal.expires_at.isoformat() if proposal.expires_at else None,
        "created_at": proposal.created_at.isoformat() if proposal.created_at else None,
    }


def _attempt_view(attempt: MonthlyCloseExecutionAttempt) -> dict:
    return {
        "attempt_id": attempt.attempt_id,
        "proposal_id": attempt.proposal_id,
        "status": attempt.status,
        "attempt_no": attempt.attempt_no,
        "request_id": attempt.request_id,
        "command_results": attempt.command_results,
        "audit_refs": attempt.audit_refs,
        "started_at": attempt.started_at.isoformat() if attempt.started_at else None,
        "finished_at": attempt.finished_at.isoformat() if attempt.finished_at else None,
    }


def _verification_view(verification: MonthlyCloseVerification) -> dict:
    return {
        "verification_id": verification.verification_id,
        "attempt_id": verification.attempt_id,
        "status": verification.status,
        "checks": verification.checks,
        "failure_code": verification.failure_code,
        "evidence_hash": verification.evidence_hash,
        "verified_at": (
            verification.verified_at.isoformat()
            if verification.verified_at
            else None
        ),
    }


async def _proposal_detail_view(
    db: AsyncSession,
    proposal: MonthlyCloseProposal,
    *,
    proposal_billing_month: str | None = None,
) -> dict:
    approvals = list(
        await db.scalars(
            select(MonthlyCloseApproval)
            .where(MonthlyCloseApproval.proposal_id == proposal.proposal_id)
            .order_by(MonthlyCloseApproval.sequence_no)
        )
    )
    attempts = list(
        await db.scalars(
            select(MonthlyCloseExecutionAttempt)
            .where(MonthlyCloseExecutionAttempt.proposal_id == proposal.proposal_id)
            .order_by(MonthlyCloseExecutionAttempt.attempt_no)
        )
    )
    attempt_ids = [attempt.attempt_id for attempt in attempts]
    verifications = (
        list(
            await db.scalars(
                select(MonthlyCloseVerification).where(
                    MonthlyCloseVerification.attempt_id.in_(attempt_ids)
                )
            )
        )
        if attempt_ids
        else []
    )
    detail = {
        **_proposal_view(proposal),
        **(
            {"proposal_billing_month": proposal_billing_month}
            if proposal_billing_month
            else {}
        ),
        "approvals": [
            {
                "approval_id": approval.approval_id,
                "decision": approval.decision,
                "decided_by": approval.decided_by,
                "maker_is_checker": approval.maker_is_checker,
                "policy_version": approval.policy_version,
                "request_id": approval.request_id,
                "reason": approval.reason,
                "decided_at": approval.decided_at.isoformat(),
            }
            for approval in approvals
        ],
        "attempts": [_attempt_view(attempt) for attempt in attempts],
        "verifications": [
            _verification_view(verification) for verification in verifications
        ],
    }
    source_adapters = {
        "service_fee_reconciliation": adapt_service_source,
        "utility_reconciliation": adapt_utility_source,
        "operating_expense_import": adapt_operating_expense_source,
    }
    adapter_loader = source_adapters.get(proposal.proposal_type)
    if adapter_loader is not None:
        cycle = await db.get(MonthlyCloseCycle, proposal.cycle_id)
        if cycle is None:
            detail["source_state"] = "blocked"
            detail["source_issues"] = ["cycle_not_found"]
        else:
            adapter = await adapter_loader(db, cycle)
            ruleset_version, calculation_version = source_contract_versions(
                proposal.proposal_type
            )
            evidence_current = source_evidence_is_current(
                cycle=cycle,
                proposal=proposal,
                current_adapter=adapter,
                ruleset_version=ruleset_version,
                calculation_version=calculation_version,
            )
            latest_attempt = attempts[-1] if attempts else None
            latest_verification = next(
                (
                    item
                    for item in reversed(verifications)
                    if latest_attempt is not None
                    and item.attempt_id == latest_attempt.attempt_id
                ),
                None,
            )
            open_source_issues = [
                item for item in adapter.issues if item.command_key is None
            ]
            approved_commands = proposal.canonical_payload.get("commands") or []
            current_commands = {
                item.business_idempotency_key: item for item in adapter.commands
            }
            executed_commands_still_current = all(
                (
                    current_commands.get(command["business_idempotency_key"])
                    is not None
                    and current_commands[command["business_idempotency_key"]].after
                    == command["after"]
                )
                for command in approved_commands
            )
            verified_current = (
                latest_attempt is not None
                and latest_attempt.status == "verified"
                and latest_verification is not None
                and latest_verification.status == "passed"
                and evidence_current
                and not open_source_issues
                and (
                    adapter.state == "completed"
                    or executed_commands_still_current
                )
            )
            detail["source_state"] = (
                "completed"
                if verified_current
                else "needs_confirmation"
                if open_source_issues or adapter.state == "needs_confirmation"
                else "blocked"
                if not evidence_current and latest_attempt is not None
                else adapter.state
            )
            detail["source_issues"] = [item.code for item in adapter.issues]
    return detail


@router.post("/{billing_month}/proposals", status_code=201)
async def create_monthly_close_proposal(
    billing_month: str,
    body: ProposalCreateBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    dedicated_ota_types = {"ota_reconciliation", "ota_appeal_adjudication"}
    if body.proposal_type in dedicated_ota_types or any(
        command.get("command_type") in dedicated_ota_types
        for command in body.commands
        if isinstance(command, dict)
    ):
        raise HTTPException(
            422,
            detail={
                "code": "ota_dedicated_builder_required",
                "message": "OTA方案或申诉裁决必须由专用入口根据当前账单事实生成",
            },
        )
    dedicated_source_types = {
        "service_fee_reconciliation",
        "utility_reconciliation",
        "operating_expense_import",
    }
    if body.proposal_type in dedicated_source_types or any(
        command.get("command_type") in dedicated_source_types
        for command in body.commands
        if isinstance(command, dict)
    ):
        raise HTTPException(
            422,
            detail={
                "code": "source_dedicated_builder_required",
                "message": "来源方案必须由专用入口根据当前原件和账本事实生成",
            },
        )
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={
                    "code": "monthly_close_not_found",
                    "message": "该月尚未开始月结",
                },
            )
        proposal = await create_proposal(
            db,
            cycle,
            current,
            body.commands,
            evidence_hash=body.evidence_hash,
            proposal_type=body.proposal_type,
            request_id=body.request_id,
        )
        return _proposal_view(proposal)
    except MonthlyCloseControlError as exc:
        raise _control_http_error(exc) from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


async def _build_source_proposal_response(
    billing_month: str,
    body: SourceProposalBody,
    current,
    db: AsyncSession,
    builder,
):
    _feature_enabled(MonthlyCloseFeature.source_adapters, current)
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    try:
        proposal = await builder(
            db, cycle.cycle_id, current, request_id=body.request_id
        )
        return _proposal_view(proposal)
    except MonthlyCloseControlError as exc:
        raise _control_http_error(exc) from exc


@router.post("/{billing_month}/service-proposals", status_code=201)
async def create_monthly_close_service_proposal(
    billing_month: str,
    body: SourceProposalBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    return await _build_source_proposal_response(
        billing_month, body, current, db, build_service_proposal
    )


@router.post("/{billing_month}/utility-proposals", status_code=201)
async def create_monthly_close_utility_proposal(
    billing_month: str,
    body: SourceProposalBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    return await _build_source_proposal_response(
        billing_month, body, current, db, build_utility_proposal
    )


@router.post("/{billing_month}/operating-expense-proposals", status_code=201)
async def create_monthly_close_operating_expense_proposal(
    billing_month: str,
    body: SourceProposalBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    return await _build_source_proposal_response(
        billing_month, body, current, db, build_operating_expense_proposal
    )


@router.get("/{billing_month}/source-proposals")
async def list_monthly_close_source_proposals(
    billing_month: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    """Recover the durable non-OTA source queue for one exact close cycle."""
    _feature_enabled(MonthlyCloseFeature.source_adapters, current)
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    source_types = (
        "service_fee_reconciliation",
        "utility_reconciliation",
        "operating_expense_import",
    )
    proposals = list(
        await db.scalars(
            select(MonthlyCloseProposal)
            .where(
                MonthlyCloseProposal.cycle_id == cycle.cycle_id,
                MonthlyCloseProposal.proposal_type.in_(source_types),
            )
            .order_by(
                MonthlyCloseProposal.proposal_type,
                MonthlyCloseProposal.proposal_version,
                MonthlyCloseProposal.proposal_id,
            )
        )
    )
    items = [
        await _proposal_detail_view(
            db, proposal, proposal_billing_month=billing_month
        )
        for proposal in proposals
    ]
    active: dict[str, dict] = {}
    for item in items:
        if item["status"] not in {"rejected", "stale", "superseded"}:
            active[item["proposal_type"]] = item
    adapter_loaders = {
        "service_fee_reconciliation": adapt_service_source,
        "utility_reconciliation": adapt_utility_source,
        "operating_expense_import": adapt_operating_expense_source,
    }
    source_states: dict[str, dict] = {}
    for proposal_type, loader in adapter_loaders.items():
        adapter = await loader(db, cycle)
        active_item = active.get(proposal_type)
        source_states[proposal_type] = {
            "state": (
                active_item.get("source_state", adapter.state)
                if active_item is not None
                else adapter.state
            ),
            "issue_codes": [item.code for item in adapter.issues],
            "change_count": len(adapter.commands),
            "proposal_id": (
                active_item["proposal_id"] if active_item is not None else None
            ),
            "upload_required": proposal_type != "service_fee_reconciliation",
        }
    return {"items": items, "active": active, "source_states": source_states}


@router.post("/{billing_month}/ota-proposals", status_code=201)
async def create_monthly_close_ota_proposal(
    billing_month: str,
    body: OtaProposalBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    """Build an immutable OTA proposal from exact server-owned recon rows."""
    _feature_enabled(MonthlyCloseFeature.source_adapters, current)
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(
            404,
            detail={
                "code": "monthly_close_not_found",
                "message": "该月尚未开始月结",
            },
        )
    try:
        proposal = await build_ota_proposal(
            db,
            cycle.cycle_id,
            body.batch_id,
            body.selected_diff_ids,
            current,
            decisions=[item.model_dump() for item in body.decisions],
            request_id=body.request_id,
        )
        return _proposal_view(proposal)
    except MonthlyCloseControlError as exc:
        raise _control_http_error(exc) from exc


@router.get("/{billing_month}/ota-batches/{batch_id}")
async def get_monthly_close_ota_batch(
    billing_month: str,
    batch_id: str,
    _current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    """Return only proposal-selection fields for one cycle-bound OTA batch."""
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    if cycle.write_control_owner != "assistant":
        raise HTTPException(
            409,
            detail={"code": "ota_control_legacy", "message": "当前月份仍由原对账入口控制写入"},
        )
    linked = await db.scalar(
        select(MonthlyCloseDocument.document_id).where(
            MonthlyCloseDocument.cycle_id == cycle.cycle_id,
            MonthlyCloseDocument.source_type == "ota_statement",
            MonthlyCloseDocument.engine_type == "billing_recon",
            MonthlyCloseDocument.engine_id == batch_id,
            MonthlyCloseDocument.is_active.is_(True),
        )
    )
    batch = await db.get(ReconBatch, batch_id) if linked is not None else None
    if batch is None or batch.bill_month != billing_month:
        raise HTTPException(
            404,
            detail={"code": "ota_batch_not_found", "message": "OTA对账批次不存在"},
        )
    diffs = list(
        await db.scalars(
            select(ReconDiff)
            .where(ReconDiff.batch_id == batch.batch_id)
            .order_by(ReconDiff.diff_class, ReconDiff.diff_id)
        )
    )
    return {
        "batch": {
            "batch_id": batch.batch_id,
            "platform": batch.platform,
            "bill_month": batch.bill_month,
            "summary_total": str(batch.summary_total),
            "row_count": batch.row_count,
            "status": batch.status,
            "error": batch.error,
            "stats": (batch.mapping or {}).get("stats", {}),
            "created_at": batch.created_at.isoformat() if batch.created_at else None,
        },
        "diffs": [
            {
                "diff_id": diff.diff_id,
                "batch_id": diff.batch_id,
                "order_id": None,
                "platform_order_id": None,
                "guest_name": None,
                "diff_class": diff.diff_class.value,
                "status": diff.status.value,
                "bill_amount": str(diff.bill_amount) if diff.bill_amount is not None else None,
                "system_amount": str(diff.system_amount) if diff.system_amount is not None else None,
                "detail": {},
            }
            for diff in diffs
        ],
    }


@router.get(
    "/{billing_month}/ota-batches/{batch_id}/diffs/{diff_id}/order-candidates"
)
async def get_monthly_close_ota_order_candidates(
    billing_month: str,
    batch_id: str,
    diff_id: str,
    q: str = Query(min_length=1, max_length=100),
    _current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    """Search an exact claim target without exposing guest/contact fields."""
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    if cycle.write_control_owner != "assistant":
        raise HTTPException(
            409,
            detail={"code": "ota_control_legacy", "message": "当前月份仍由原对账入口控制写入"},
        )
    batch = await db.get(ReconBatch, batch_id)
    diff = await db.get(ReconDiff, diff_id)
    linked_document = await db.scalar(
        select(MonthlyCloseDocument.document_id).where(
            MonthlyCloseDocument.cycle_id == cycle.cycle_id,
            MonthlyCloseDocument.source_type == "ota_statement",
            MonthlyCloseDocument.engine_type == "billing_recon",
            MonthlyCloseDocument.engine_id == batch_id,
            MonthlyCloseDocument.is_active.is_(True),
        )
    )
    if (
        batch is None
        or diff is None
        or diff.batch_id != batch_id
        or batch.bill_month != billing_month
        or batch.status != "parsed"
        or linked_document is None
    ):
        raise HTTPException(
            404,
            detail={"code": "ota_diff_not_found", "message": "有效OTA差异不存在"},
        )
    if diff.status.value != "pending" or diff.diff_class.value != "manual_review":
        raise HTTPException(
            409,
            detail={"code": "ota_diff_not_actionable", "message": "该差异当前不需要人工关联"},
        )
    pattern = f"%{q.strip()}%"
    orders = list(
        await db.scalars(
            select(Order)
            .where(
                Order.is_deleted.is_(False),
                Order.order_id.ilike(pattern) | Order.platform_order_id.ilike(pattern),
            )
            .order_by(Order.check_out_date.desc(), Order.order_id)
            .limit(20)
        )
    )
    return {
        "items": [
            {
                "order_id": order.order_id,
                "channel": order.channel.value,
                "check_in_date": order.check_in_date.isoformat(),
                "check_out_date": order.check_out_date.isoformat(),
                "expected_revenue": (
                    f"{order.expected_revenue:.2f}"
                    if order.expected_revenue is not None
                    else None
                ),
                "platform_linked": bool(order.platform_order_id),
            }
            for order in orders
        ]
    }


@router.post("/{billing_month}/finalization-proposals", status_code=201)
async def create_monthly_close_finalization_proposal(
    billing_month: str,
    body: FinalizationProposalBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.finalize_cycle)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={
                    "code": "monthly_close_not_found",
                    "message": "该月尚未开始月结",
                },
            )
        proposal = await build_finalization_proposal(
            db,
            cycle,
            current,
            request_id=body.request_id,
        )
        return _proposal_view(proposal)
    except MonthlyCloseControlError as exc:
        raise _control_http_error(exc) from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.get("/{billing_month}/proposals/{proposal_id}")
async def get_monthly_close_proposal(
    billing_month: str,
    proposal_id: str,
    _current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    _cycle, proposal = await _proposal_in_month(db, billing_month, proposal_id)
    return await _proposal_detail_view(db, proposal)


@router.post("/{billing_month}/proposals/{proposal_id}/approve")
async def approve_monthly_close_proposal(
    billing_month: str,
    proposal_id: str,
    body: ProposalDecisionBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.approve_proposal)),
    db: AsyncSession = Depends(get_db),
):
    _cycle, proposal = await _proposal_in_month(db, billing_month, proposal_id)
    try:
        approval = await approve_proposal(
            db,
            proposal,
            current,
            request_id=body.request_id,
            reason=body.reason,
        )
        return {
            "approval_id": approval.approval_id,
            "proposal_id": approval.proposal_id,
            "decision": approval.decision,
            "maker_is_checker": approval.maker_is_checker,
            "request_id": approval.request_id,
        }
    except MonthlyCloseControlError as exc:
        raise _control_http_error(exc) from exc


@router.post("/{billing_month}/proposals/{proposal_id}/reject")
async def reject_monthly_close_proposal(
    billing_month: str,
    proposal_id: str,
    body: ProposalDecisionBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.approve_proposal)),
    db: AsyncSession = Depends(get_db),
):
    _cycle, proposal = await _proposal_in_month(db, billing_month, proposal_id)
    try:
        approval = await reject_proposal(
            db,
            proposal,
            current,
            request_id=body.request_id,
            reason=body.reason or "",
        )
        return {
            "approval_id": approval.approval_id,
            "proposal_id": approval.proposal_id,
            "decision": approval.decision,
            "request_id": approval.request_id,
        }
    except MonthlyCloseControlError as exc:
        raise _control_http_error(exc) from exc


@router.post("/{billing_month}/proposals/{proposal_id}/execute")
async def execute_monthly_close_proposal(
    billing_month: str,
    proposal_id: str,
    body: ProposalExecuteBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.execute_proposal)),
    db: AsyncSession = Depends(get_db),
):
    _proposal_execution_enabled()
    _cycle, proposal = await _proposal_in_month(db, billing_month, proposal_id)
    try:
        attempt = await execute_approved_proposal(
            db,
            proposal,
            current,
            request_id=body.request_id,
        )
        return _attempt_view(attempt)
    except MonthlyCloseControlError as exc:
        raise _control_http_error(exc) from exc


@router.post("/{billing_month}/attempts/{attempt_id}/verify")
async def verify_monthly_close_attempt(
    billing_month: str,
    attempt_id: str,
    body: VerificationBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.execute_proposal)),
    db: AsyncSession = Depends(get_db),
):
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(
            404,
            detail={
                "code": "monthly_close_not_found",
                "message": "该月尚未开始月结",
            },
        )
    attempt = await db.scalar(
        select(MonthlyCloseExecutionAttempt).where(
            MonthlyCloseExecutionAttempt.attempt_id == attempt_id,
            MonthlyCloseExecutionAttempt.cycle_id == cycle.cycle_id,
        )
    )
    if attempt is None:
        raise HTTPException(
            404,
            detail={"code": "attempt_not_found", "message": "执行记录不存在"},
        )
    try:
        verification = await verify_attempt(
            db, attempt, current, request_id=body.request_id
        )
        return _verification_view(verification)
    except MonthlyCloseControlError as exc:
        raise _control_http_error(exc) from exc


@router.post("/{billing_month}/actions/ota/reconcile-settled")
async def reconcile_monthly_close_settled_ota_appeal(
    billing_month: str,
    body: OtaAppealReconcileBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.execute_proposal)),
    db: AsyncSession = Depends(get_db),
):
    _proposal_execution_enabled()
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    try:
        issue = await reconcile_settled_ota_issue(
            db,
            cycle.cycle_id,
            body.issue_id,
            current,
            request_id=body.request_id,
        )
        await db.commit()
        await db.refresh(issue)
        return {
            "issue_id": issue.issue_id,
            "status": issue.status,
            "verification_id": issue.verification_id,
            "evidence_hash": issue.last_seen_evidence_hash,
            "resolution_note": issue.resolution_note,
        }
    except MonthlyCloseControlError as exc:
        raise _control_http_error(exc) from exc


@router.post(
    "/{billing_month}/ota-appeal-adjudication-proposals", status_code=201
)
async def create_monthly_close_ota_appeal_adjudication_proposal(
    billing_month: str,
    body: OtaAppealAdjudicationBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    _feature_enabled(MonthlyCloseFeature.source_adapters, current)
    later_cycle = await get_cycle_by_month(db, billing_month)
    if later_cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    try:
        proposal = await build_ota_appeal_adjudication_proposal(
            db,
            later_cycle.cycle_id,
            body.candidate_id,
            body.action,
            body.reason,
            current,
            identity_choice_id=body.identity_choice_id,
            request_id=body.request_id,
        )
        proposal_cycle = await db.get(MonthlyCloseCycle, proposal.cycle_id)
        return {
            **_proposal_view(proposal),
            "proposal_billing_month": proposal_cycle.billing_month,
        }
    except MonthlyCloseControlError as exc:
        raise _control_http_error(exc) from exc


@router.get("/{billing_month}/ota-appeal-adjudication-proposals")
async def list_monthly_close_ota_appeal_adjudication_proposals(
    billing_month: str,
    _current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    later_cycle = await get_cycle_by_month(db, billing_month)
    if later_cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    if later_cycle.write_control_owner != "assistant":
        raise HTTPException(
            409,
            detail={"code": "ota_control_legacy", "message": "当前月份仍由原对账入口控制写入"},
        )
    proposals = list(
        await db.scalars(
            select(MonthlyCloseProposal)
            .where(
                MonthlyCloseProposal.proposal_type == "ota_appeal_adjudication"
            )
            .order_by(MonthlyCloseProposal.created_at.desc())
        )
    )
    scoped: list[MonthlyCloseProposal] = []
    for proposal in proposals:
        commands = (
            proposal.canonical_payload.get("commands", [])
            if isinstance(proposal.canonical_payload, dict)
            else []
        )
        if len(commands) != 1 or not isinstance(commands[0], dict):
            continue
        before = commands[0].get("before")
        if (
            isinstance(before, dict)
            and before.get("later_cycle_id") == later_cycle.cycle_id
            and before.get("later_billing_month") == billing_month
        ):
            scoped.append(proposal)
    cycle_ids = {proposal.cycle_id for proposal in scoped}
    cycles = {
        item.cycle_id: item.billing_month
        for item in await db.scalars(
            select(MonthlyCloseCycle).where(
                MonthlyCloseCycle.cycle_id.in_(cycle_ids or {"__none__"})
            )
        )
    }
    items = [
        await _proposal_detail_view(
            db,
            proposal,
            proposal_billing_month=cycles.get(proposal.cycle_id),
        )
        for proposal in scoped
    ]

    def bucket(item: dict) -> str:
        if any(
            verification.get("status") == "passed"
            for verification in item.get("verifications", [])
        ) or any(
            attempt.get("status") == "verified"
            for attempt in item.get("attempts", [])
        ):
            return "verified"
        if item.get("status") in {"rejected", "stale", "superseded"} or any(
            attempt.get("status")
            in {"failed_confirmed", "remediation_required", "unknown"}
            for attempt in item.get("attempts", [])
        ):
            return "rejected"
        return "pending"

    priority = {"pending": 0, "rejected": 1, "verified": 2}
    items.sort(
        key=lambda item: (
            priority[bucket(item)],
            item.get("created_at") or "",
            item["proposal_id"],
        )
    )
    grouped: dict[tuple[str, str], dict] = {}
    for item in items:
        before = item["canonical_payload"]["commands"][0]["before"]
        issue_id = str(before.get("issue_id") or "unknown-issue")
        candidate_id = str(before.get("candidate_id") or "unknown-candidate")
        group = grouped.setdefault(
            (issue_id, candidate_id),
            {
                "issue_id": issue_id,
                "candidate_id": candidate_id,
                "pending": [],
                "rejected": [],
                "verified": [],
            },
        )
        group[bucket(item)].append(item)
    return {"items": items, "groups": list(grouped.values())}


@router.get("/{billing_month}/ota-appeal-settlement-candidates")
async def get_monthly_close_ota_appeal_settlement_candidates(
    billing_month: str,
    _current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    if cycle.write_control_owner != "assistant":
        raise HTTPException(
            409,
            detail={"code": "ota_control_legacy", "message": "当前月份仍由原对账入口控制写入"},
        )
    return {
        "items": await list_ota_appeal_settlement_candidates(db, cycle.cycle_id)
    }


@router.get("/{billing_month}")
async def get_monthly_close(
    billing_month: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.prepare_proposal)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    if cycle is None:
        raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
    return await build_cycle_view(db, cycle)


@router.get("/{billing_month}/projection")
async def get_monthly_close_projection(
    billing_month: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.view_cycle)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    if cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    locked_cycle = await lock_cycle_for_snapshot(db, cycle.cycle_id)
    if locked_cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    projection = await build_role_cycle_projection(db, locked_cycle, current)
    snapshot_sequence = await cycle_event_high_watermark(db, locked_cycle.cycle_id)
    projection["snapshot_through_sequence"] = public_cycle_cursor(
        locked_cycle.cycle_id, current, snapshot_sequence
    )
    return projection


def _event_cursor(last_event_id: str | None, query_cursor: str | None) -> str:
    if last_event_id is not None and last_event_id.strip():
        return last_event_id.strip()
    return (query_cursor or "").strip()


@router.get("/{billing_month}/events")
async def get_monthly_close_events(
    billing_month: str,
    request: Request,
    after_sequence: str | None = None,
    limit: int = 50,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    current=Depends(require_monthly_close_action(MonthlyCloseAction.view_cycle)),
    db: AsyncSession = Depends(get_db),
):
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    cursor = _event_cursor(last_event_id, after_sequence)
    try:
        page = await list_cycle_events(db, cycle.cycle_id, current, cursor, limit)
    except EventCursorExpired as exc:
        raise HTTPException(
            410,
            detail={
                "code": "EVENT_CURSOR_EXPIRED",
                "message": "事件游标已过期，请重新获取月结快照",
            },
        ) from exc
    except EventCursorRefreshRequired as exc:
        raise HTTPException(
            410,
            detail={
                "code": "EVENT_CURSOR_REFRESH_REQUIRED",
                "message": "事件游标密钥已更新，请重新获取月结快照",
            },
        ) from exc
    except EventCursorInvalid as exc:
        raise HTTPException(
            422,
            detail={"code": "EVENT_CURSOR_INVALID", "message": "事件游标无效"},
        ) from exc
    if "text/event-stream" in request.headers.get("accept", ""):
        chunks = []
        for event in page.events:
            data = json.dumps(
                event.to_dict(), ensure_ascii=False, separators=(",", ":")
            )
            chunks.append(
                f"id: {event.event_id}\nevent: {event.kind}\ndata: {data}\n\n"
            )
        if page.advanced:
            chunks.append(
                f"id: {page.next_cursor}\nevent: cursor\ndata: {{}}\n\n"
            )
        return Response(
            content="".join(chunks),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )
    return page.to_dict()


@router.get("/{billing_month}/progress-details")
async def get_monthly_close_progress_details(
    billing_month: str,
    step_key: str,
    document_id: str | None = Query(default=None, max_length=80),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=50),
    expected_evidence_hash: str | None = Query(default=None, pattern=r"^[0-9a-f]{64}$"),
    current=Depends(require_monthly_close_action(MonthlyCloseAction.view_cycle)),
    db: AsyncSession = Depends(get_db),
):
    from app.services.monthly_close.progress_details import read_progress_details

    if current["role"] != "admin":
        raise HTTPException(403, detail="仅管理员可查看完整月结事项")
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail="该月尚未开始月结")
        return await read_progress_details(
            db, cycle, actor_role=current["role"], step_key=step_key,
            offset=offset, limit=limit,
            expected_evidence_hash=expected_evidence_hash, document_id=document_id, actor=current,
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc


@router.get("/{billing_month}/messages/{run_id}/export")
async def export_monthly_close_result(
    billing_month: str, run_id: str, request: Request,
    format: Literal["txt", "md", "csv", "json", "xlsx", "docx", "pdf", "html"] = "txt",
    current=Depends(require_monthly_close_action(MonthlyCloseAction.view_cycle)),
    db: AsyncSession = Depends(get_db),
):
    import asyncio
    from app.services.monthly_close.result_delivery import export_document
    from app.services.monthly_close.result_output import render_document, MEDIA
    _feature_enabled(MonthlyCloseFeature.natural_language, current)
    if request.headers.get("X-Privacy-Mode") == "1":
        raise HTTPException(409, detail="隐私演示模式禁止导出查询快照")
    if current["role"] != "admin":
        raise HTTPException(403, detail="仅管理员可导出对账查询")
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(404, detail="该月尚未开始月结")
    try:
        document = await export_document(db, cycle, current, run_id)
    except PermissionError as exc:
        raise HTTPException(403, detail="你的权限已变化，请重新登录") from exc
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, detail="查询结果超出当前导出范围，请缩小范围后重新查询") from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    data = await asyncio.to_thread(render_document, document, format)
    # Recheck live authority after rendering; revoked sessions cannot finish an export.
    from app.services.monthly_close.result_delivery import source_reply
    try:
        await source_reply(db, cycle, current, run_id)
    except PermissionError as exc:
        raise HTTPException(403, detail="你的权限已变化，本次导出已停止") from exc
    except LookupError as exc:
        raise HTTPException(404, detail=str(exc)) from exc
    filename = quote(f"{billing_month}-对账核对结果.{format}")
    return Response(content=data, media_type=MEDIA[format], headers={
        "Content-Disposition": f"attachment; filename*=UTF-8''{filename}",
        "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "sandbox; default-src 'none'; style-src 'unsafe-inline'",
    })


@router.get("/{billing_month}/messages")
async def get_monthly_close_chat_history(
    billing_month: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.view_cycle)),
    db: AsyncSession = Depends(get_db),
):
    _feature_enabled(MonthlyCloseFeature.natural_language, current)
    if current["role"] != "admin":
        raise HTTPException(403, detail="仅管理员可查看处理对话")
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(404, detail="该月尚未开始月结")
    try:
        return {"replies": await list_monthly_close_chat_history(db, cycle, current)}
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.get("/{billing_month}/task")
async def get_monthly_close_task(
    billing_month: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.view_cycle)),
    db: AsyncSession = Depends(get_db),
):
    from app.services.monthly_close.task_runtime import read_task
    _feature_enabled(MonthlyCloseFeature.natural_language, current)
    if current["role"] != "admin":
        raise HTTPException(403, detail="仅管理员可查看月结任务")
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(404, detail="该月尚未开始月结")
    return {"task": await read_task(db, cycle.cycle_id, current["user_id"])}


@router.post("/{billing_month}/task")
async def update_monthly_close_task(
    billing_month: str,
    body: MonthlyCloseTaskBody,
    redis: RedisClient,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.view_cycle)),
    db: AsyncSession = Depends(get_db),
):
    from app.services.monthly_close.task_runtime import start_task, resume_task, pause_task
    _feature_enabled(MonthlyCloseFeature.natural_language, current)
    if current["role"] != "admin":
        raise HTTPException(403, detail="仅管理员可操作月结任务")
    await enforce_rate_limit(redis, f"monthly-close-task:{current['user_id']}", limit=20, window_seconds=60)
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(404, detail="该月尚未开始月结")
    kwargs = {"expected_revision": body.expected_revision}
    if body.action == "start":
        task = await start_task(db, cycle.cycle_id, current["user_id"], body.goal, **kwargs)
    elif body.action == "resume":
        task = await resume_task(db, cycle.cycle_id, current["user_id"], **kwargs)
    else:
        task = await pause_task(db, cycle.cycle_id, current["user_id"], **kwargs)
    # The durable row is enough: beat dispatches it even if this tab closes now.
    return {"task": task}


async def _answer_monthly_close_with_task(db, cycle, current, body, *, on_progress=None):
    from app.services.monthly_close.task_chat import answer_task_command
    reply = await answer_task_command(db, cycle, current, body.text, body.attachment_ids, body.context_run_id)
    if reply is not None:
        return reply
    kwargs = {"on_progress": on_progress} if on_progress is not None else {}
    reply = await answer_monthly_close_message(db, cycle, current, body.text, body.attachment_ids, body.context_run_id, **kwargs)
    # Controlled plan revisions/cancellation and committed actions can unblock
    # tasks. Viewing an attachment or asking a read question must not wake them.
    controlled_tool = getattr(reply, "tool", None) in {"cleaning_work_chat", "financial_case", "order_identity_chat"}
    facts = getattr(reply, "facts", {}) or {}
    if reply.intent in {"action_result", "action_plan"} or (controlled_tool and facts.get("state") in {"cancelled", "conflict"}):
        await _wake_monthly_close_task(db, cycle.cycle_id)
    return reply


async def _answer_monthly_close_stream(session_factory, cycle_id, current, body, progress):
    """Own the connection until streaming work has finished or been cancelled.

    FastAPI 0.115 closes yield dependencies before consuming StreamingResponse.
    Reusing that request session can reopen an unowned connection. The factory
    uses the request bind so test overrides and isolated deployments stay scoped.
    """
    async with session_factory() as stream_db:
        cycle = await stream_db.get(MonthlyCloseCycle, cycle_id)
        if cycle is None:
            raise HTTPException(404, detail="该月尚未开始月结")
        try:
            actor = await authorize_current_monthly_close_actor(
                stream_db, current, MonthlyCloseAction.view_cycle,
                MonthlyClosePermissionContext(cycle_id=cycle_id, subject_cycle_id=cycle_id),
            )
        except MonthlyClosePermissionDenied as exc:
            raise HTTPException(403, detail="权限不足") from exc
        # Fresh role/deactivation state replaces the route-time token snapshot.
        refreshed = {**current, "user_id": actor.user_id,
                     "role": getattr(actor.role, "value", actor.role),
                     "is_active": actor.is_active}
        _feature_enabled(MonthlyCloseFeature.natural_language, refreshed)
        return await _answer_monthly_close_with_task(
            stream_db, cycle, refreshed, body, on_progress=progress,
        )


@router.post("/{billing_month}/messages")
async def post_monthly_close_message(
    billing_month: str,
    body: MonthlyCloseMessageBody,
    request: Request,
    redis: RedisClient,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.view_cycle)),
    db: AsyncSession = Depends(get_db),
):
    _feature_enabled(MonthlyCloseFeature.natural_language, current)
    await enforce_rate_limit(
        redis,
        f"monthly-close-assistant:messages:{current['user_id']}",
        limit=20,
        window_seconds=60,
    )
    cycle = await get_cycle_by_month(db, billing_month)
    if cycle is None:
        raise HTTPException(
            404,
            detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
        )
    if "text/event-stream" in request.headers.get("accept", ""):
        from app.services.monthly_close.streaming import stream_reply

        stream_sessions = async_sessionmaker(bind=db.bind, expire_on_commit=False, autoflush=False)
        cycle_id = cycle.cycle_id

        async def answer(progress):
            return await _answer_monthly_close_stream(
                stream_sessions, cycle_id, current, body, progress,
            )

        return StreamingResponse(stream_reply(answer), media_type="text/event-stream",
                                 headers={"Cache-Control":"no-cache, no-transform", "X-Accel-Buffering":"no"})
    try:
        reply = await _answer_monthly_close_with_task(db, cycle, current, body)
    except AssistantAttachmentNotFound as exc:
        raise HTTPException(
            404,
            detail={
                "code": "ASSISTANT_ATTACHMENT_NOT_FOUND",
                "message": "附件不存在",
            },
        ) from exc
    except AssistantAuthorizationChanged as exc:
        raise HTTPException(403, detail="权限不足") from exc
    return reply.to_dict()


@router.get("/{billing_month}/projection/documents/{document_id}")
async def get_monthly_close_projection_document(
    billing_month: str,
    document_id: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.view_cycle)),
    db: AsyncSession = Depends(get_db),
):
    not_found = HTTPException(
        404,
        detail={"code": "projection_object_not_found", "message": "对象不存在"},
    )
    try:
        cycle = await get_cycle_by_month(db, billing_month)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    if cycle is None:
        raise not_found
    document = await get_visible_projection_document(db, cycle, current, document_id)
    if document is None:
        raise not_found
    return document.to_dict()


@router.post("/{billing_month}/steps/{step_key}/confirm")
async def confirm_monthly_close_step(
    billing_month: str,
    step_key: str,
    body: ConfirmStepBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.finalize_cycle)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
        await confirm_step(
            db,
            cycle,
            step_key,
            current["user_id"],
            expected_evidence_hash=body.expected_evidence_hash,
            note=body.note,
        )
        return await build_cycle_view(db, cycle)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post("/{billing_month}/reopen")
async def reopen_monthly_close(
    billing_month: str,
    body: ReopenBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.finalize_cycle)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
        cycle = await reopen_cycle(
            db, cycle, current["user_id"], reason=body.reason
        )
        return await build_cycle_view(db, cycle)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post("/{billing_month}/documents")
async def upload_monthly_close_document(
    billing_month: str,
    source_type: str,
    replaces_document_id: str | None = None,
    file: UploadFile = File(...),
    current=Depends(require_monthly_close_action(MonthlyCloseAction.upload_source)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
        cycle = await require_cycle_writable(db, cycle)
        if file.size and file.size > MAX_MONTHLY_CLOSE_DOCUMENT_BYTES:
            raise MonthlyCloseDocumentError("document_too_large", "文件超过 10MB", 413)
        data = await file.read()
        document = await store_document(
            db,
            cycle,
            source_type=normalize_current_source_type(source_type),
            filename=file.filename or "document.xlsx",
            mime_type=file.content_type or "application/octet-stream",
            data=data,
            user_id=current["user_id"],
            replaces_document_id=replaces_document_id,
        )
        return {
            "document_id": document.document_id,
            "source_type": document.source_type,
            "filename": document.filename,
            "byte_size": document.byte_size,
            "sha256": document.sha256,
            "is_active": document.is_active,
            "processing_status": document.processing_status,
        }
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc


@router.post("/{billing_month}/staff-documents", status_code=201)
async def upload_staff_monthly_close_document(
    billing_month: str,
    source_type: str,
    file: UploadFile = File(...),
    request_id: str | None = Header(default=None, alias="Idempotency-Key"),
    current=Depends(
        require_monthly_close_action(
            MonthlyCloseAction.upload_source,
            low_role_upload_entrypoint=True,
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    """Accept staff files through the durable receipt/job/outbox/event intake path."""
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        cycle = await require_cycle_writable(db, cycle)
        if file.size and file.size > MAX_MONTHLY_CLOSE_DOCUMENT_BYTES:
            raise MonthlyCloseDocumentError("document_too_large", "文件超过 10MB", 413)
        receipt = await receive_monthly_close_file(
            db,
            cycle,
            request_id=request_id or f"staff-{uuid4().hex}",
            filename=file.filename or "document.xlsx",
            mime_type=file.content_type or "application/octet-stream",
            data=await file.read(),
            origin="admin",
            user_id=current["user_id"],
            source_type=source_type,
            submitted_label="员工职责月结资料",
        )
        return durable_receipt_view(receipt)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc
    except MonthlyCloseInboxError as exc:
        raise _inbox_error(exc) from exc


@router.post("/{billing_month}/staff-inbox", status_code=201)
async def receive_staff_monthly_close_inbox_file(
    billing_month: str,
    file: UploadFile = File(...),
    request_id: str | None = Header(default=None, alias="Idempotency-Key"),
    current=Depends(
        require_monthly_close_action(
            MonthlyCloseAction.upload_source,
            low_role_upload_entrypoint=True,
        )
    ),
    db: AsyncSession = Depends(get_db),
):
    """Let staff send an unclassified workbook through the same assistant composer."""
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        cycle = await require_cycle_writable(db, cycle)
        if file.size and file.size > MAX_MONTHLY_CLOSE_DOCUMENT_BYTES:
            raise MonthlyCloseDocumentError("document_too_large", "文件超过 10MB", 413)
        receipt = await receive_monthly_close_file(
            db,
            cycle,
            request_id=request_id or f"staff-inbox-{uuid4().hex}",
            filename=file.filename or "document.xlsx",
            mime_type=file.content_type or "application/octet-stream",
            data=await file.read(),
            origin="admin",
            user_id=current["user_id"],
            submitted_label="员工月结资料",
        )
        return durable_receipt_view(receipt)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc
    except MonthlyCloseInboxError as exc:
        raise _inbox_error(exc) from exc


@router.post(
    "/{billing_month}/documents/classify",
    response_model=SourceClassification,
)
async def classify_monthly_close_document(
    billing_month: str,
    file: UploadFile = File(...),
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        await require_cycle_writable(db, cycle)
        if file.size and file.size > MAX_MONTHLY_CLOSE_DOCUMENT_BYTES:
            raise MonthlyCloseDocumentError("document_too_large", "文件超过 10MB", 413)
        data = await file.read()
        validate_document(data, file.filename or "document.xlsx")
        return await classify_monthly_close_source(
            data, file.filename or "document.xlsx"
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc


@router.get("/{billing_month}/inbox")
async def get_monthly_close_inbox(
    billing_month: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        items = await list_inbox_items(db, cycle.cycle_id)
        item_ids = [item.item_id for item in items]
        document_ids = [item.document_id for item in items if item.document_id]
        jobs = (
            list(
                await db.scalars(
                    select(MonthlyCloseProcessingJob)
                    .where(
                        MonthlyCloseProcessingJob.cycle_id == cycle.cycle_id,
                        MonthlyCloseProcessingJob.job_type == "classify_document",
                        MonthlyCloseProcessingJob.subject_id.in_(item_ids),
                    )
                    .order_by(MonthlyCloseProcessingJob.created_at.desc())
                )
            )
            if item_ids
            else []
        )
        job_by_item: dict[str, MonthlyCloseProcessingJob] = {}
        for job in jobs:
            job_by_item.setdefault(job.subject_id, job)
        analyses = (
            list(
                await db.scalars(
                    select(MonthlyCloseDocumentAnalysis)
                    .where(
                        MonthlyCloseDocumentAnalysis.cycle_id == cycle.cycle_id,
                        MonthlyCloseDocumentAnalysis.document_id.in_(document_ids),
                    )
                    .order_by(MonthlyCloseDocumentAnalysis.generation.desc())
                )
            )
            if document_ids
            else []
        )
        analysis_by_document: dict[str, MonthlyCloseDocumentAnalysis] = {}
        for analysis in analyses:
            analysis_by_document.setdefault(analysis.document_id, analysis)
        result = []
        for item in items:
            view = inbox_item_view(item)
            job = job_by_item.get(item.item_id)
            analysis = (
                analysis_by_document.get(item.document_id)
                if item.document_id and item.status == "confirmed"
                else None
            )
            classification_status = (
                str(job.payload.get("processing_state")) if job else None
            )
            view.update(
                {
                    "storage_status": "stored",
                    "processing_status": (
                        analysis.status if analysis else classification_status
                    ),
                    "processing_job_id": (
                        analysis.job_id if analysis else (job.job_id if job else None)
                    ),
                    "classification_status": classification_status,
                    "classification_job_id": job.job_id if job else None,
                    "analysis_status": analysis.status if analysis else None,
                    "analysis_job_id": analysis.job_id if analysis else None,
                    "analysis_generation": (
                        analysis.generation if analysis else None
                    ),
                    "analysis_result": analysis.result if analysis else None,
                    "analysis_error": (
                        analysis.manual_action if analysis else None
                    ),
                }
            )
            result.append(view)
        return result
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post("/{billing_month}/inbox", status_code=201)
async def receive_monthly_close_inbox_file(
    billing_month: str,
    file: UploadFile = File(...),
    request_id: str | None = Header(default=None, alias="Idempotency-Key"),
    current=Depends(require_monthly_close_action(MonthlyCloseAction.upload_source)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        cycle = await require_cycle_writable(db, cycle)
        if file.size and file.size > MAX_MONTHLY_CLOSE_DOCUMENT_BYTES:
            raise MonthlyCloseDocumentError("document_too_large", "文件超过 10MB", 413)
        receipt = await receive_monthly_close_file(
            db,
            cycle,
            request_id=request_id or f"server-{uuid4().hex}",
            filename=file.filename or "document.xlsx",
            mime_type=file.content_type or "application/octet-stream",
            data=await file.read(),
            origin="admin",
            user_id=current["user_id"],
        )
        return durable_receipt_view(receipt)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc
    except MonthlyCloseInboxError as exc:
        raise _inbox_error(exc) from exc


@router.post("/{billing_month}/inbox/{item_id}/classify")
async def classify_monthly_close_inbox_file(
    billing_month: str,
    item_id: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        cycle = await require_cycle_writable(db, cycle)
        return inbox_item_view(
            await classify_inbox_item(
                db, cycle, item_id, user_id=current["user_id"]
            )
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseInboxError as exc:
        raise _inbox_error(exc) from exc


@router.patch("/{billing_month}/inbox/{item_id}")
async def correct_monthly_close_inbox_source(
    billing_month: str,
    item_id: str,
    body: InboxSourceBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        cycle = await require_cycle_writable(db, cycle)
        return inbox_item_view(
            await set_inbox_source(
                db,
                cycle,
                item_id,
                source_type=body.source_type,
                user_id=current["user_id"],
            )
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc
    except MonthlyCloseInboxError as exc:
        raise _inbox_error(exc) from exc


@router.post("/{billing_month}/inbox/{item_id}/confirm")
async def confirm_monthly_close_inbox_file(
    billing_month: str,
    item_id: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        cycle = await require_cycle_writable(db, cycle)
        return inbox_item_view(
            await confirm_inbox_item(
                db, cycle, item_id, user_id=current["user_id"]
            )
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc
    except MonthlyCloseInboxError as exc:
        raise _inbox_error(exc) from exc


@router.delete("/{billing_month}/inbox/{item_id}", status_code=204)
async def permanently_delete_monthly_close_inbox_file(
    billing_month: str,
    item_id: str,
    current=Depends(
        require_monthly_close_action(MonthlyCloseAction.permanently_delete_source)
    ),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        cycle = await require_cycle_writable(db, cycle)
        await permanently_delete_inbox_item(
            db, cycle, item_id, user_id=current["user_id"]
        )
        return Response(status_code=204)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseInboxError as exc:
        raise _inbox_error(exc) from exc


@router.get("/{billing_month}/intake-links")
async def get_monthly_close_intake_links(
    billing_month: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.manage_intake_links)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        return [
            intake_link_view(link)
            for link in await list_intake_links(db, cycle.cycle_id)
        ]
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post("/{billing_month}/intake-links")
async def create_monthly_close_intake_link(
    billing_month: str,
    body: IntakeLinkCreateBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.manage_intake_links)),
    db: AsyncSession = Depends(get_db),
):
    _feature_enabled(MonthlyCloseFeature.external_intake)
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        cycle = await require_cycle_writable(db, cycle)
        link, token = await create_intake_link(
            db,
            cycle,
            label=body.label,
            source_type=body.source_type,
            user_id=current["user_id"],
        )
        return {**intake_link_view(link), "token": token}
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc
    except MonthlyCloseInboxError as exc:
        raise _inbox_error(exc) from exc


@router.post("/{billing_month}/intake-links/{link_id}/revoke")
async def revoke_monthly_close_intake_link(
    billing_month: str,
    link_id: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.manage_intake_links)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        cycle = await require_cycle_writable(db, cycle)
        return intake_link_view(
            await revoke_intake_link(
                db, cycle.cycle_id, link_id, user_id=current["user_id"]
            )
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseInboxError as exc:
        raise _inbox_error(exc) from exc


@router.get("/intake/{token}")
async def get_public_monthly_close_intake(
    token: str,
    request: Request,
    redis: RedisClient,
    db: AsyncSession = Depends(get_db),
):
    _feature_enabled(MonthlyCloseFeature.external_intake)
    try:
        client_ip = request.client.host if request.client else "unknown"
        await enforce_rate_limit(
            redis,
            f"monthly_close_intake_public:{client_ip}",
            limit=120,
            window_seconds=3600,
        )
        link, cycle = await resolve_intake_link(db, token)
        return {
            "billing_month": cycle.billing_month,
            "label": link.label,
            "source_type": link.source_type,
            "source_label": SOURCE_LABELS.get(link.source_type, "月结资料"),
            "expires_at": link.expires_at.isoformat(),
        }
    except MonthlyCloseInboxError as exc:
        raise _inbox_error(exc) from exc


@router.post("/intake/{token}")
async def upload_public_monthly_close_intake(
    token: str,
    request: Request,
    redis: RedisClient,
    file: UploadFile = File(...),
    request_id: str | None = Header(default=None, alias="Idempotency-Key"),
    db: AsyncSession = Depends(get_db),
):
    _feature_enabled(MonthlyCloseFeature.external_intake)
    try:
        client_ip = request.client.host if request.client else "unknown"
        await enforce_rate_limit(
            redis,
            f"monthly_close_intake_public:{client_ip}",
            limit=120,
            window_seconds=3600,
        )
        link, cycle = await resolve_intake_link(db, token)
        # Serialize public intake with all admin writes for the same cycle. After
        # waiting for the lock, re-check the bearer link so a concurrent revoke
        # or month completion cannot slip one final upload through.
        cycle = await require_cycle_writable(db, cycle)
        link, cycle = await resolve_intake_link(db, token, for_update=True)
        await enforce_rate_limit(
            redis,
            f"monthly_close_intake:{link.link_id}:{client_ip}",
            limit=30,
            window_seconds=3600,
        )
        if file.size and file.size > MAX_MONTHLY_CLOSE_DOCUMENT_BYTES:
            raise MonthlyCloseDocumentError("document_too_large", "文件超过 10MB", 413)
        normalized_request_id = request_id or f"public-{uuid4().hex}"
        receipt = await receive_monthly_close_file(
            db,
            cycle,
            request_id=normalized_request_id,
            filename=file.filename or "document.xlsx",
            mime_type=file.content_type or "application/octet-stream",
            data=await file.read(),
            origin="external",
            user_id=None,
            source_type=link.source_type,
            submitted_label=link.label,
            intake_link_id=link.link_id,
        )
        item = await db.get(MonthlyCloseInboxItem, receipt.item_id)
        if item is None:
            raise RuntimeError("durable public receipt is missing its inbox item")
        link.last_uploaded_at = datetime.now(timezone.utc)
        await db.commit()
        conflict = (
            item.status == "needs_review"
            and item.last_error is not None
            and "资料类型冲突" in item.last_error
        )
        response = {
            "receipt_code": receipt.public_receipt_code,
            "status": item.status,
            "message": (
                "原文件已保存，但不同收件链接声明的资料类型冲突；"
                "请工作人员确认，无需重新上传。"
                if conflict
                else "文件已收到，工作人员确认后会进入本月对账。"
            ),
        }
        await _wake_monthly_close_task(db, cycle.cycle_id)
        return response
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseInboxError as exc:
        raise _inbox_error(exc) from exc


@router.post("/{billing_month}/sources/{source_type}/not-applicable")
async def set_monthly_close_source_not_applicable(
    billing_month: str,
    source_type: str,
    body: NotApplicableBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
        cycle = await require_cycle_writable(db, cycle)
        requirement = await mark_source_not_applicable(
            db,
            cycle,
            source_type=source_type,
            reason=body.reason,
            user_id=current["user_id"],
        )
        return {
            "source_type": requirement.source_type,
            "state": requirement.state,
            "not_applicable_reason": requirement.not_applicable_reason,
        }
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc


@router.post(
    "/{billing_month}/documents/{document_id}/ota-appeal-candidates/{candidate_id}/archive"
)
async def archive_monthly_close_ota_candidate_document(
    billing_month: str,
    document_id: str,
    candidate_id: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        cycle = await require_cycle_writable(db, cycle)
        candidates = await list_ota_appeal_settlement_candidates(db, cycle.cycle_id)
        candidate = next(
            (item for item in candidates if item.get("candidate_id") == candidate_id),
            None,
        )
        if candidate is None:
            raise HTTPException(
                409,
                detail={"code": "ota_appeal_candidate_stale", "message": "到账候选已经变化，请刷新后重试"},
            )
        if candidate.get("later_document_id") != document_id:
            raise HTTPException(
                409,
                detail={"code": "ota_candidate_document_mismatch", "message": "到账候选与待归档文件不一致"},
            )
        if candidate.get("recovery_kind") != "archive_reupload":
            raise HTTPException(
                409,
                detail={"code": "ota_candidate_archive_not_allowed", "message": "当前问题不是文件行错误，不能归档到账证据"},
            )
        document = await db.scalar(
            select(MonthlyCloseDocument).where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.document_id == document_id,
                MonthlyCloseDocument.is_active.is_(True),
                MonthlyCloseDocument.source_type == "ota_statement",
                MonthlyCloseDocument.engine_type == "billing_recon",
                MonthlyCloseDocument.engine_id == candidate.get("later_batch_id"),
            )
        )
        if document is None:
            raise HTTPException(
                409,
                detail={"code": "ota_appeal_candidate_stale", "message": "到账候选文件已归档或被替换"},
            )
        archived = await archive_document(
            db, cycle, document_id=document_id, user_id=current["user_id"]
        )
        return {
            "candidate_id": candidate_id,
            "document_id": archived.document_id,
            "is_active": archived.is_active,
        }
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc


@router.post("/{billing_month}/documents/{document_id}/archive")
async def archive_monthly_close_document(
    billing_month: str,
    document_id: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.finalize_cycle)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
        cycle = await require_cycle_writable(db, cycle)
        document = await archive_document(
            db, cycle, document_id=document_id, user_id=current["user_id"]
        )
        return {"document_id": document.document_id, "is_active": document.is_active}
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc


@router.get("/documents/{document_id}/download")
async def download_monthly_close_document(
    document_id: str,
    current=Depends(get_current_user),
    db: AsyncSession = Depends(get_db),
):
    # Preserve the existing admin-only download permission while hiding whether
    # a guessed attachment ID exists from every non-admin employee role.
    database_user = await db.get(User, current["user_id"])
    if (
        database_user is None
        or not database_user.is_active
        or database_user.role.value != "admin"
    ):
        raise HTTPException(
            404,
            detail={"code": "document_not_found", "message": "文件不存在"},
        )
    document = await get_document(db, document_id)
    if document is None:
        raise HTTPException(404, detail={"code": "document_not_found", "message": "文件不存在"})
    safe_filename = quote(document.filename.replace("\r", "").replace("\n", ""))
    return Response(
        content=document.content,
        media_type=document.mime_type,
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{safe_filename}"},
    )


@router.post(
    "/{billing_month}/documents/{document_id}/service/analyze",
    response_model=ServiceStatementAnalysis,
)
async def analyze_monthly_close_service_document(
    billing_month: str,
    document_id: str,
    body: ServiceAnalyzeBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle, document = await _cycle_document(db, billing_month, document_id)
        if document.source_type not in {"cleaning_statement", "linen_statement"}:
            raise HTTPException(
                422,
                detail={"code": "not_service_statement", "message": "该文件不是保洁或布草对账单"},
            )
        analysis = await analyze_service_statement(
            document.content,
            document.filename,
            document.source_type,
            mapping=body.mapping,
            billing_month=billing_month,
        )
        if analysis.work_log is not None:
            from app.services.monthly_close.cleaning_work_log import (
                compare_cleaning_work_log, parse_cleaning_work_log,
            )
            entries = parse_cleaning_work_log(document.content, document.filename, billing_month)
            analysis.work_log = await compare_cleaning_work_log(db, entries, billing_month, document.document_id)
            from app.services.monthly_close.cleaning_recognition import refresh_cleaning_recognition
            if current["role"] == "admin":
                await refresh_cleaning_recognition(db, cycle, document, current)
                await db.commit()
        return analysis
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ServiceStatementError as exc:
        raise _service_statement_error(exc) from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except (openai.APIStatusError, openai.APIConnectionError, ServiceMappingError) as exc:
        raise HTTPException(
            503,
            detail={"code": "service_mapping_unavailable", "message": "表格结构识别暂不可用，可重试或人工填写列号"},
        ) from exc


class WorkImportConfirmBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    preview_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    request_id: str = Field(min_length=8, max_length=80, pattern=r"^[A-Za-z0-9_-]+$")


@router.post("/{billing_month}/documents/{document_id}/work-log/preview")
async def preview_monthly_cleaning_work_import(billing_month: str, document_id: str, current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)), db: AsyncSession = Depends(get_db)):
    from app.services.monthly_close.cleaning_work_import import preview_work_import
    try:
        cycle, document = await _cycle_document(db, billing_month, document_id, source_type="cleaning_statement")
        return await preview_work_import(db, cycle, document, current["user_id"])
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except ServiceStatementError as exc:
        raise _service_statement_error(exc) from exc


@router.post("/{billing_month}/documents/{document_id}/work-log/confirm")
async def confirm_monthly_cleaning_work_import(billing_month: str, document_id: str, body: WorkImportConfirmBody, current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)), db: AsyncSession = Depends(get_db)):
    from app.services.monthly_close.cleaning_work_import import confirm_work_import
    try:
        cycle, document = await _cycle_document(db, billing_month, document_id, source_type="cleaning_statement")
        return await confirm_work_import(db, cycle, document, current["user_id"], body.preview_hash, body.request_id)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "work_log_concurrent_change", "message": "记录已被其他操作更新，请重新预览"}) from exc
    except MonthlyCloseConflict as exc:
        await db.rollback()
        raise _http_conflict(exc) from exc
    except ServiceStatementError as exc:
        await db.rollback()
        raise _service_statement_error(exc) from exc


class WorkResolutionBody(BaseModel):
    model_config = ConfigDict(extra="forbid")
    service_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    room_ref: str = Field(min_length=1, max_length=80)
    service_type: Literal["cleaning", "instay_cleaning"]
    decision: Literal["exclude_system", "count_once", "accept_table"]
    reason: str = Field(min_length=2, max_length=1000)


class WorkResolutionConfirmBody(WorkResolutionBody, WorkImportConfirmBody):
    pass


@router.post("/{billing_month}/documents/{document_id}/work-log/resolution/preview")
async def preview_monthly_cleaning_resolution(billing_month: str, document_id: str, body: WorkResolutionBody, current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)), db: AsyncSession = Depends(get_db)):
    from app.services.monthly_close.cleaning_work_resolution import preview_work_resolution
    try:
        cycle, document = await _cycle_document(db, billing_month, document_id, source_type="cleaning_statement")
        return await preview_work_resolution(db, cycle, document, current["user_id"], body.model_dump())
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except ServiceStatementError as exc:
        raise _service_statement_error(exc) from exc


@router.post("/{billing_month}/documents/{document_id}/work-log/resolution/confirm")
async def confirm_monthly_cleaning_resolution(billing_month: str, document_id: str, body: WorkResolutionConfirmBody, current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)), db: AsyncSession = Depends(get_db)):
    from app.services.monthly_close.cleaning_work_resolution import confirm_work_resolution
    try:
        cycle, document = await _cycle_document(db, billing_month, document_id, source_type="cleaning_statement")
        return await confirm_work_resolution(db, cycle, document, current["user_id"], body.model_dump(exclude={"preview_hash", "request_id"}), body.preview_hash, body.request_id)
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail={"code": "resolution_concurrent_change", "message": "记录已被其他操作更新，请重新预览"}) from exc
    except MonthlyCloseConflict as exc:
        await db.rollback()
        raise _http_conflict(exc) from exc
    except ServiceStatementError as exc:
        await db.rollback()
        raise _service_statement_error(exc) from exc


@router.post("/{billing_month}/documents/{document_id}/service/confirm")
async def confirm_monthly_close_service_document(
    billing_month: str,
    document_id: str,
    body: ServiceConfirmBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle, document = await _cycle_document(db, billing_month, document_id)
        await require_cycle_writable(db, cycle)
        if document.source_type not in {"cleaning_statement", "linen_statement"}:
            raise HTTPException(
                422,
                detail={"code": "not_service_statement", "message": "该文件不是保洁或布草对账单"},
            )
        if document.source_type == "cleaning_statement":
            from app.services.monthly_close.cleaning_work_log import parse_cleaning_work_log
            if parse_cleaning_work_log(document.content, document.filename, billing_month) is not None:
                raise _service_statement_error(ServiceStatementError("这是打扫工作记录，请查看房间记录核对结果，不能作为金额账单导入"))
        lines = await process_service_document(
            db,
            document,
            mapping=body.mapping.to_statement_mapping(),
            billing_month=billing_month,
        )
        if document.processing_status == "rejected":
            message = document.processing_error or "表格字段映射无法解析"
            await db.rollback()
            raise _service_statement_error(ServiceStatementError(message))
        await complete_document_analysis_manually(
            db,
            document,
            actor_id=current["user_id"],
            confirmation={
                "source_type": document.source_type,
                "mapping": body.mapping.model_dump(mode="json"),
                "line_count": len(lines),
            },
        )
        await log_action_tx(
            db,
            current["user_id"],
            "monthly_close.service_mapping.confirm",
            "monthly_close_document",
            document.document_id,
            after_data={
                "cycle_id": cycle.cycle_id,
                "billing_month": billing_month,
                "mapping": body.mapping.model_dump(mode="json"),
                "line_count": len(lines),
            },
        )
        await db.commit()
        await db.refresh(document)
        return {
            "document_id": document.document_id,
            "processing_status": document.processing_status,
            "line_count": len(lines),
        }
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post(
    "/{billing_month}/documents/{document_id}/operating-expense/analyze",
    response_model=OperatingExpenseAnalysis,
)
async def analyze_monthly_close_operating_expense_document(
    billing_month: str,
    document_id: str,
    body: OperatingExpenseAnalyzeBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle, document = await _cycle_document(
            db,
            billing_month,
            document_id,
            source_type="operating_expenses",
        )
        room_rows = list(
            (await db.execute(select(Room.room_id, Room.room_name))).tuples()
        )
        valid_room_ids = {room_id for room_id, _ in room_rows}
        confirmed_mapping = body.mapping
        mapping_origin = "administrator"
        if confirmed_mapping is None:
            remembered = await find_remembered_mapping(
                db,
                source_type="operating_expenses",
                data=document.content,
                filename=document.filename,
            )
            if remembered is not None:
                confirmed_mapping = OperatingExpenseMappingCoordinates.model_validate(
                    remembered
                )
                mapping_origin = "remembered"
        analysis = await analyze_operating_expense_statement(
            document.content,
            document.filename,
            billing_month,
            valid_room_ids=valid_room_ids,
            room_aliases=build_room_aliases(room_rows),
            mapping=confirmed_mapping,
            mapping_origin=mapping_origin,
        )
        replacement_context = await operating_replacement_context(
            db,
            cycle,
            document,
            analysis.mapping.to_expense_mapping(),
        )
        return analysis.model_copy(
            update={
                "replacement_context": (
                    OperatingExpenseReplacementContext.model_validate(
                        replacement_context
                    )
                    if replacement_context is not None
                    else None
                )
            }
        )
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except (openai.APIStatusError, openai.APIConnectionError, OperatingExpenseMappingError) as exc:
        raise HTTPException(
            503,
            detail={
                "code": "operating_expense_mapping_unavailable",
                "message": "表格结构识别暂不可用，可重试或人工确认列号",
            },
        ) from exc


@router.post("/{billing_month}/documents/{document_id}/operating-expense/confirm")
async def confirm_monthly_close_operating_expense_document(
    billing_month: str,
    document_id: str,
    body: OperatingExpenseConfirmBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle, document = await _cycle_document(
            db,
            billing_month,
            document_id,
            source_type="operating_expenses",
        )
        expected_control_version = cycle.control_version
        cycle = await require_cycle_writable(db, cycle)
        if cycle.write_control_owner == "assistant":
            mapping = body.mapping.to_expense_mapping()
            predecessor_events, event_links = await confirmed_operating_event_metadata(
                db,
                cycle,
                document,
                mapping,
                body.mapping.predecessor_rows,
            )
            document.metadata_ = {
                **(document.metadata_ or {}),
                "mapping": {
                    "sheet": mapping.sheet,
                    "header_row": mapping.header_row,
                    "columns": mapping.columns,
                    "category_values": mapping.category_values,
                    "payer_values": mapping.payer_values,
                },
                "operating_expense_event_links": event_links,
                "operating_expense_mapping_confirmed": True,
                "operating_predecessor_event_mappings": predecessor_events,
            }
            remember_document_mapping(
                document,
                data=document.content,
                filename=document.filename,
                mapping=mapping,
            )
            await log_action_tx(
                db,
                current["user_id"],
                "monthly_close.operating_expense_mapping.confirm",
                "monthly_close_document",
                document.document_id,
                after_data={
                    "billing_month": billing_month,
                    "cycle_id": cycle.cycle_id,
                    "mapping": document.metadata_["mapping"],
                },
            )
            await db.commit()
            return {
                "document_id": document.document_id,
                "mapping_confirmed": True,
                "proposal_required": True,
            }
        cycle = await require_cycle_write_control(
            db,
            cycle.cycle_id,
            expected_version=expected_control_version,
            owner="legacy",
        )
        await _reauthorize_monthly_close_action(
            db,
            current,
            MonthlyCloseAction.execute_proposal,
            cycle=cycle,
            source_type=document.source_type,
            subject_type="document",
            subject_id=document.document_id,
            uploaded_by=document.uploaded_by,
        )
        return await import_operating_expense_document(
            db,
            cycle,
            document,
            current["user_id"],
            mapping=body.mapping.to_expense_mapping(),
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseRolloutError as exc:
        raise _rollout_http_error(exc) from exc
    except MonthlyCloseDocumentError as exc:
        await db.rollback()
        raise _document_error(exc) from exc


@router.post(
    "/{billing_month}/documents/{document_id}/utility/analyze",
    response_model=UtilityAnalysis,
)
async def analyze_monthly_close_utility_document(
    billing_month: str,
    document_id: str,
    body: UtilityAnalyzeBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle, document = await _cycle_document(db, billing_month, document_id)
        legacy_utility_contract = await _cycle_uses_legacy_utility_contract(db, cycle)
        expected_role = (
            "receipt"
            if document.source_type == "utility_receipt"
            and legacy_utility_contract
            else "expense"
            if document.source_type in {"utility_receipt", "utility_expense"}
            else None
        )
        if expected_role is None:
            raise HTTPException(
                422,
                detail={"code": "not_utility_document", "message": "该文件不是水电对账资料"},
            )
        mapping = body.mapping
        suggested_by = "administrator"
        if mapping is None:
            remembered = await find_remembered_mapping(
                db,
                source_type=document.source_type,
                data=document.content,
                filename=document.filename,
            )
            stored = (document.metadata_ or {}).get("utility_mapping")
            candidate = stored if isinstance(stored, dict) else remembered
            if candidate is not None:
                mapping = UtilityColumnMapping.model_validate(candidate)
                suggested_by = "remembered"
        preview = build_workbook_preview(document.content, document.filename)
        try:
            inspected = await inspect_workbook_with_ai(
                WorkbookInput(document.filename, document.content),
                mapping,
                target_month=billing_month,
                allow_ai=settings.MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED,
            )
        except WorkbookInspectionError:
            if body.mapping is not None:
                raise
            first_sheet = preview[0]
            amount_field = (
                "receipt_amount" if expected_role == "receipt" else "expense_amount"
            )
            return UtilityAnalysis(
                mapping=UtilityColumnMapping(
                    role=expected_role,
                    sheet=first_sheet["name"],
                    header_row=0,
                    columns={"date": 0, amount_field: 1},
                ),
                months=[],
                record_count=0,
                total_amount="0.00",
                suggested_by="administrator",
                sheets=preview,
            )
        if inspected.role != expected_role:
            raise WorkbookInspectionError("资料类型与确认的流水角色不一致")
        table = inspected.sheets[0]
        coordinates = UtilityColumnMapping(
            role=inspected.role,
            sheet=table.sheet,
            header_row=table.header_row - 1,
            columns=table.columns,
        )
        if mapping is None:
            suggested_by = (
                "ai" if inspected.mapping_status == "mapped_by_ai" else "deterministic"
            )
        record_count, total_amount = summarize_inspected_file(
            inspected,
            billing_month,
        )
        return UtilityAnalysis(
            mapping=coordinates,
            months=inspected.months,
            record_count=record_count,
            total_amount=str(total_amount),
            suggested_by=suggested_by,
            sheets=preview,
        )
    except WorkbookInspectionError as exc:
        raise HTTPException(
            422,
            detail={"code": "utility_mapping_invalid", "message": str(exc)},
        ) from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post("/{billing_month}/documents/{document_id}/utility/confirm")
async def confirm_monthly_close_utility_document(
    billing_month: str,
    document_id: str,
    body: UtilityConfirmBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle, document = await _cycle_document(db, billing_month, document_id)
        await require_cycle_writable(db, cycle)
        legacy_utility_contract = await _cycle_uses_legacy_utility_contract(db, cycle)
        expected_role = (
            "receipt"
            if document.source_type == "utility_receipt"
            and legacy_utility_contract
            else "expense"
            if document.source_type in {"utility_receipt", "utility_expense"}
            else None
        )
        if expected_role is None or body.mapping.role != expected_role:
            raise WorkbookInspectionError("资料类型与确认的流水角色不一致")
        inspected = await inspect_workbook_with_ai(
            WorkbookInput(document.filename, document.content),
            body.mapping,
            target_month=billing_month,
        )
        if billing_month not in inspected.months:
            raise WorkbookInspectionError(
                f"账单中没有 {billing_month} 的有效日期，请确认月份或日期列。"
            )
        document.metadata_ = {
            **(document.metadata_ or {}),
            "utility_mapping": body.mapping.model_dump(mode="json"),
            "utility_mapping_confirmed": True,
        }
        remember_document_mapping(
            document,
            data=document.content,
            filename=document.filename,
            mapping=body.mapping,
        )
        await complete_document_analysis_manually(
            db,
            document,
            actor_id=current["user_id"],
            confirmation={
                "source_type": document.source_type,
                "mapping": body.mapping.model_dump(mode="json"),
                "months": inspected.months,
            },
        )
        document.processing_status = "processed"
        document.processing_error = None
        await log_action_tx(
            db,
            current["user_id"],
            "monthly_close.utility_mapping.confirm",
            "monthly_close_document",
            document.document_id,
            after_data={
                "cycle_id": cycle.cycle_id,
                "billing_month": billing_month,
                "mapping": body.mapping.model_dump(mode="json"),
            },
        )
        await db.commit()
        return {
            "document_id": document.document_id,
            "role": inspected.role,
            "months": inspected.months,
        }
    except WorkbookInspectionError as exc:
        await db.rollback()
        raise HTTPException(
            422,
            detail={"code": "utility_mapping_invalid", "message": str(exc)},
        ) from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post("/{billing_month}/actions/utility/run")
async def run_monthly_close_utility(
    billing_month: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.execute_proposal)),
    db: AsyncSession = Depends(get_db),
):
    _proposal_execution_enabled()
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
        expected_control_version = cycle.control_version
        cycle = await require_cycle_writable(db, cycle)
        if cycle.write_control_owner == "assistant":
            raise HTTPException(
                409,
                detail={
                    "code": "utility_proposal_required",
                    "message": "当前月份的水电写入必须通过专用方案批准执行",
                },
            )
        cycle = await require_cycle_write_control(
            db,
            cycle.cycle_id,
            expected_version=expected_control_version,
            owner="legacy",
        )
        batch = await run_utility_from_documents(db, cycle, current["user_id"])
        return {
            "batch_id": batch.batch_id,
            "month": batch.month,
            "status": batch.status,
        }
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseRolloutError as exc:
        raise _rollout_http_error(exc) from exc
    except MonthlyCloseDocumentError as exc:
        raise _document_error(exc) from exc
    except WorkbookInspectionError as exc:
        await db.rollback()
        raise HTTPException(
            422,
            detail={"code": "utility_workbook_invalid", "message": str(exc)},
        ) from exc


@router.post("/{billing_month}/actions/service-fees/reconcile")
async def reconcile_monthly_close_service_fees(
    billing_month: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.execute_proposal)),
    db: AsyncSession = Depends(get_db),
):
    _proposal_execution_enabled()
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
        expected_control_version = cycle.control_version
        cycle = await require_cycle_writable(db, cycle)
        if cycle.write_control_owner == "assistant":
            raise HTTPException(
                409,
                detail={
                    "code": "service_proposal_required",
                    "message": "当前月份的服务费写入必须通过专用方案批准执行",
                },
            )
        cycle = await require_cycle_write_control(
            db,
            cycle.cycle_id,
            expected_version=expected_control_version,
            owner="legacy",
        )
        return await reconcile_service_fees_from_documents(
            db, cycle, current["user_id"]
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseRolloutError as exc:
        raise _rollout_http_error(exc) from exc


@router.post("/{billing_month}/documents/{document_id}/ota/analyze")
async def analyze_monthly_close_ota_document(
    billing_month: str,
    document_id: str,
    body: OtaAnalyzeBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        _, document = await _cycle_document(
            db, billing_month, document_id, source_type="ota_statement"
        )
        return await analyze_bill(
            file=_upload_from_document(document),
            mapping_json=(
                json.dumps(body.coordinates, ensure_ascii=False)
                if body.coordinates is not None
                else None
            ),
            allow_ai=settings.MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED,
            current=current,
            db=db,
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post("/{billing_month}/actions/operating-expenses/import")
async def import_monthly_close_operating_expenses(
    billing_month: str,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.execute_proposal)),
    db: AsyncSession = Depends(get_db),
):
    _proposal_execution_enabled()
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
        expected_control_version = cycle.control_version
        cycle = await require_cycle_writable(db, cycle)
        if cycle.write_control_owner == "assistant":
            raise HTTPException(
                409,
                detail={
                    "code": "operating_expense_proposal_required",
                    "message": "当前月份的运营支出写入必须通过专用方案批准执行",
                },
            )
        cycle = await require_cycle_write_control(
            db,
            cycle.cycle_id,
            expected_version=expected_control_version,
            owner="legacy",
        )
        return await import_operating_expense_documents(
            db, cycle, current["user_id"]
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseRolloutError as exc:
        raise _rollout_http_error(exc) from exc
    except MonthlyCloseDocumentError as exc:
        await db.rollback()
        raise _document_error(exc) from exc


@router.post("/{billing_month}/documents/{document_id}/ota/confirm")
async def confirm_monthly_close_ota_document(
    billing_month: str,
    document_id: str,
    body: OtaConfirmBody,
    current=Depends(require_monthly_close_action(MonthlyCloseAction.confirm_mapping)),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle, document = await _cycle_document(
            db, billing_month, document_id, source_type="ota_statement"
        )
        cycle = await require_cycle_writable(db, cycle)
        result = await confirm_bill(
            file=_upload_from_document(document),
            file_fingerprint=document.sha256,
            mapping_json=json.dumps(body.coordinates, ensure_ascii=False),
            platform_scope=body.platform_scope,
            remember_layout=body.remember_layout,
            current=current,
            db=db,
            _assistant_intake_cycle_id=cycle.cycle_id,
            _assistant_intake_document_id=document.document_id,
        )
        batch_id = result["batch"]["batch_id"]
        document.engine_type = "billing_recon"
        document.engine_id = batch_id
        document.processing_status = "processed"
        document.processing_error = None
        document.metadata_ = {
            **(document.metadata_ or {}),
            "billing_recon_batch_id": batch_id,
            "billing_month": billing_month,
        }
        await db.flush()
        batch = await db.get(ReconBatch, batch_id)
        if batch is None:
            raise MonthlyCloseControlError(
                "ota_batch_not_found", "OTA对账批次不存在"
            )
        await stage_ota_appeal_settlement_candidates(
            db, cycle, document, batch
        )
        await log_action_tx(
            db,
            current["user_id"],
            "monthly_close.ota.confirm",
            "monthly_close_document",
            document.document_id,
            after_data={
                "cycle_id": cycle.cycle_id,
                "billing_month": billing_month,
                "batch_id": batch_id,
            },
        )
        await db.commit()
        return result
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
