# backend/app/api/v1/billing_recon.py
"""账单对账（billing-recon）端点。全部 admin-only。

AI 只在 upload 里做列映射（app/services/billing_recon/ai_mapping.py，
model=DeepSeek deepseek-chat）；对账/写库全是确定性代码（services/billing_recon/）。
"""
import asyncio
import logging
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

import openai
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.deps import get_db, require_current_db_role, require_role
from app.models.monthly_close import MonthlyCloseCycle, MonthlyCloseDocument
from app.models.recon import ReconBatch, ReconDiff
from app.services.billing_recon.ai_mapping import AiMappingError
from app.services.billing_recon.analysis import (
    MappingAnalysis,
    MappingCoordinates,
    MappingIssue,
    MappingQuality,
    PlatformScope,
    analyze_mapping,
    build_layout_signature,
    materialize_mapping,
    recognize_mapping_coordinates,
)
from app.services.billing_recon.engine import (
    BillRejected, ReconWriteControlled, apply_diff_action, claim_match,
    lock_recon_write_control, run_recon, source_namespace,
)
from app.services.billing_recon.layout_templates import (
    coordinates_for_workbook,
    find_layout_template,
    save_layout_template,
)
from app.services.billing_recon.parser import (
    BillParseError,
    aggregate_orders,
    extract_bill_rows,
    infer_bill_month,
    load_workbook_rows,
)
from app.services.billing_recon.upload import (
    ProcessingBatchState, get_or_create_processing_batch, mark_processing_batch_failed,
    upload_fingerprint, validate_bill_container,
)
from app.services.billing_recon.summary import build_live_summary, review_batch
from app.services.audit import log_action_tx
from app.services.monthly_close.adapters import build_ota_proposal
from app.services.monthly_close.control import MonthlyCloseControlError

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/billing-recon", tags=["billing-recon"])

_MAX_UPLOAD = 10 * 1024 * 1024


def _direct_confirm_context() -> None:
    """Keep the internal assistant intake marker out of the public request surface."""
    return None


def _direct_confirm_document_context() -> None:
    """Keep the exact assistant document marker out of public confirmation."""
    return None


def _direct_analysis_ai_enabled() -> bool:
    """Direct billing analysis retains its established optional AI fallback."""
    return True


_PlatformSuggestion = Literal[
    "ctrip_family", "meituan", "fliggy", "douyin", "tujia", "other",
]


class WorkbookAnalysisOut(BaseModel):
    """Read-only workbook analysis returned before an administrator confirms a mapping."""

    file_fingerprint: str
    layout_signature: str
    coordinates: MappingCoordinates
    field_confidence: dict[str, float] = Field(default_factory=dict)
    platform_suggestion: _PlatformSuggestion
    platform_scope: PlatformScope
    quality: MappingQuality
    warnings: list[str] = Field(default_factory=list)
    errors: list[MappingIssue] = Field(default_factory=list)
    preview: list[dict[str, str | float | None]] = Field(default_factory=list)
    template_hit: bool
    needs_confirmation: bool = True


def _api_error(
    code: str, message: str, status_code: int, *, field: str | None = None,
) -> HTTPException:
    """Return a safe, structured API error without echoing workbook contents."""
    detail = {"code": code, "message": message}
    if field is not None:
        detail["field"] = field
    return HTTPException(status_code, detail=detail)


def _first_analysis_error(analysis: MappingAnalysis) -> HTTPException | None:
    """Turn safe deterministic analysis failures into the public error contract."""
    if not analysis.errors:
        return None
    issue = analysis.errors[0]
    fallback_field = "summary_cell" if issue.code == "TOTAL_MISMATCH" else "mapping_json"
    return _api_error(issue.code, issue.message, 422, field=issue.field or fallback_field)


async def _read_bill_file(file: UploadFile) -> tuple[bytes, str]:
    """Read and preflight a bill upload for both analyze and a future confirm endpoint.

    The helper deliberately has no database interactions.  It rejects the filename/size
    before workbook parsing and never places raw workbook values in an exception payload.
    """
    filename = file.filename or ""
    if file.size and file.size > _MAX_UPLOAD:
        raise _api_error("FILE_INVALID", "文件超过 10MB", 413, field="file")
    data = await file.read()
    if len(data) > _MAX_UPLOAD:
        raise _api_error("FILE_INVALID", "文件超过 10MB", 413, field="file")
    try:
        validate_bill_container(data, filename)
    except BillParseError:
        raise _api_error("FILE_INVALID", "请上传有效的 xls/xlsx 账单文件", 422, field="file") from None
    return data, upload_fingerprint(data)


def _scope_for_suggestion(suggestion: _PlatformSuggestion) -> PlatformScope:
    """Turn an advisory AI suggestion into the confirmation UI's safe default scope."""
    return PlatformScope.all_ota if suggestion == "other" else PlatformScope(suggestion)


def _suggestion_for_scope(scope: PlatformScope) -> _PlatformSuggestion:
    """Templates persist a confirmed scope; all-OTA is presented as advisory ``other``."""
    return "other" if scope is PlatformScope.all_ota else scope.value


@router.post("/analyze", response_model=WorkbookAnalysisOut)
async def analyze_bill(
    file: UploadFile = File(...),
    mapping_json: str | None = Form(None),
    allow_ai: bool = Depends(_direct_analysis_ai_enabled),
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
) -> WorkbookAnalysisOut:
    """Analyze a bill without writing batches, diffs, settlements, or template usage."""
    data, fingerprint = await _read_bill_file(file)
    filename = file.filename or ""
    try:
        sheets, datemode = await asyncio.to_thread(load_workbook_rows, data, filename)
    except BillParseError:
        raise _api_error("FILE_INVALID", "无法解析该 Excel 文件", 422) from None

    signature = build_layout_signature(sheets)
    template_coordinates: MappingCoordinates | None = None
    if mapping_json is not None:
        try:
            coordinates = MappingCoordinates.model_validate_json(mapping_json)
        except ValidationError:
            raise _api_error(
                "MAPPING_INCOMPLETE", "字段映射无效", 422, field="mapping_json",
            ) from None
        # An administrator's edited coordinates are deterministic input.  They
        # must not be overwritten by a remembered template or sent back to AI.
        template_hit = False
        platform_scope = PlatformScope.all_ota
        platform_suggestion = "other"
        confidence: dict[str, float] = {}
    else:
        template = await find_layout_template(db, signature)
        template_coordinates = (
            coordinates_for_workbook(template.mapping, list(sheets))
            if template is not None
            else None
        )
        template_hit = template_coordinates is not None

    if mapping_json is None and template_coordinates is not None:
        # A lookup is intentionally read-only: do not update use_count/last_used_at
        # until a later confirmation endpoint commits a successfully reused layout.
        coordinates = template_coordinates
        platform_scope = PlatformScope(template.platform_scope)
        platform_suggestion = _suggestion_for_scope(platform_scope)
        confidence: dict[str, float] = {}
    elif mapping_json is None:
        deterministic_coordinates = recognize_mapping_coordinates(sheets)
        if deterministic_coordinates is not None:
            coordinates = deterministic_coordinates
            platform_scope = PlatformScope.all_ota
            platform_suggestion = "other"
            confidence = {}
        elif not allow_ai:
            raise _api_error(
                "MAPPING_INCOMPLETE",
                "未识别出账单列，请人工填写订单号、客人、入住、离店和结算金额对应的列号",
                422,
                field="mapping_json",
            )
        else:
            # Keep this import lazy so the established test monkeypatch boundary remains
            # app.services.billing_recon.ai_mapping.ai_column_mapping.
            from app.services.billing_recon.ai_mapping import ai_column_mapping

            if not settings.DEEPSEEK_API_KEY:
                raise _api_error("AI_UNAVAILABLE", "AI 暂不可用，可稍后重试", 503)
            try:
                suggestion = await ai_column_mapping(sheets)
            except (openai.APIStatusError, openai.APIConnectionError, AiMappingError):
                raise _api_error("AI_UNAVAILABLE", "AI 暂不可用，可稍后重试", 503) from None
            coordinates = suggestion.coordinates
            platform_suggestion = suggestion.platform_suggestion
            platform_scope = _scope_for_suggestion(platform_suggestion)
            confidence = suggestion.field_confidence

    analysis: MappingAnalysis = analyze_mapping(sheets, datemode, coordinates)
    return WorkbookAnalysisOut(
        file_fingerprint=fingerprint,
        layout_signature=signature,
        coordinates=analysis.coordinates,
        field_confidence=confidence,
        platform_suggestion=platform_suggestion,
        platform_scope=platform_scope,
        quality=analysis.quality,
        warnings=analysis.warnings,
        errors=analysis.errors,
        preview=analysis.preview,
        template_hit=template_hit,
    )


def _batch_out(b: ReconBatch) -> dict:
    return {
        "batch_id": b.batch_id, "platform": b.platform, "bill_month": b.bill_month,
        "summary_total": str(b.summary_total), "row_count": b.row_count,
        "status": b.status, "error": b.error,
        "stats": (b.mapping or {}).get("stats", {}),
        "warnings": (b.mapping or {}).get("warnings", []),
        "summary": (b.mapping or {}).get("summary", {}),
        "diagnosis": (b.mapping or {}).get("ai_diagnosis", {}),
        "ai_status": (b.mapping or {}).get("ai_status", "failed"),
        "reviewed_at": (b.mapping or {}).get("reviewed_at"),
        "reviewed_by": (b.mapping or {}).get("reviewed_by"),
        "filename": (b.mapping or {}).get("filename"),
        "archived_at": (b.mapping or {}).get("archived_at"),
        "created_at": b.created_at.isoformat() if b.created_at else None,
    }


def _diff_out(d: ReconDiff) -> dict:
    return {
        "diff_id": d.diff_id, "batch_id": d.batch_id, "order_id": d.order_id,
        "platform_order_id": d.platform_order_id, "guest_name": d.guest_name,
        "diff_class": d.diff_class.value, "status": d.status.value,
        "bill_amount": str(d.bill_amount) if d.bill_amount is not None else None,
        "system_amount": str(d.system_amount) if d.system_amount is not None else None,
        "detail": d.detail or {},
    }


async def _batch_diffs(db: AsyncSession, batch_id: str) -> list[ReconDiff]:
    rows = await db.execute(
        select(ReconDiff).where(ReconDiff.batch_id == batch_id).order_by(ReconDiff.diff_class, ReconDiff.diff_id)
    )
    return list(rows.scalars().all())


async def _batch_write_control(
    db: AsyncSession, batch_id: str
) -> tuple[ReconBatch | None, MonthlyCloseCycle | None, bool]:
    """Lock the authoritative cycle before its exact batch to avoid lock inversion."""
    subject = await db.scalar(
        select(ReconBatch).where(ReconBatch.batch_id == batch_id)
    )
    if subject is None:
        return None, None, False
    cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.billing_month == subject.bill_month)
        .with_for_update(of=MonthlyCloseCycle)
        .execution_options(populate_existing=True)
    )
    batch = await db.scalar(
        select(ReconBatch)
        .where(ReconBatch.batch_id == batch_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if batch is None:
        return None, None, False
    if cycle is not None and cycle.billing_month != batch.bill_month:
        return batch, None, False
    linked = bool(
        cycle is not None
        and await db.scalar(
            select(MonthlyCloseDocument.document_id).where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.source_type == "ota_statement",
                MonthlyCloseDocument.engine_type == "billing_recon",
                MonthlyCloseDocument.engine_id == batch.batch_id,
                MonthlyCloseDocument.is_active.is_(True),
            ).limit(1)
        )
    )
    return batch, cycle, linked


def _assistant_controlled_error(cycle: MonthlyCloseCycle) -> HTTPException:
    return _api_error(
        "OTA_ASSISTANT_CONTROLLED",
        f"{cycle.billing_month} 的OTA调整由月结方案控制，请在月结中心生成并审批方案",
        409,
    )


async def _reject_assistant_controlled_write(
    db: AsyncSession, batch_id: str
) -> tuple[ReconBatch | None, MonthlyCloseCycle | None, bool]:
    batch, cycle, linked = await _batch_write_control(db, batch_id)
    if cycle is not None and cycle.write_control_owner == "assistant":
        raise _assistant_controlled_error(cycle)
    return batch, cycle, linked


def _proposal_out(proposal) -> dict:
    return {
        "proposal_id": proposal.proposal_id,
        "cycle_id": proposal.cycle_id,
        "proposal_type": proposal.proposal_type,
        "status": proposal.status,
        "canonical_payload": proposal.canonical_payload,
        "impact_snapshot": proposal.impact_snapshot,
        "approval_policy_snapshot": proposal.approval_policy_snapshot,
    }


async def _delegate_assistant_decision(
    db: AsyncSession,
    diff: ReconDiff,
    current: dict,
    decision: dict,
    request_id: str | None,
):
    _batch, cycle, linked = await _batch_write_control(db, diff.batch_id)
    if cycle is None or cycle.write_control_owner != "assistant":
        return None
    if not linked:
        raise _assistant_controlled_error(cycle)
    try:
        proposal = await build_ota_proposal(
            db,
            cycle.cycle_id,
            diff.batch_id,
            [],
            current,
            decisions=[{"diff_id": diff.diff_id, **decision}],
            request_id=request_id or f"OTALEG:{uuid4().hex[:24]}",
        )
    except MonthlyCloseControlError as exc:
        raise _api_error(
            exc.code,
            exc.message,
            409,
        ) from exc
    return JSONResponse(status_code=201, content=_proposal_out(proposal))


def _diff_class_counts(diffs: list[ReconDiff]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for d in diffs:
        key = d.diff_class.value
        counts[key] = counts.get(key, 0) + 1
    return counts


def _batch_state_error(batch: ReconBatch) -> HTTPException | None:
    """Keep incomplete lifecycle rows out of read and human-action paths."""
    if batch.status == "processing":
        return HTTPException(
            409,
            detail={
                "code": "PROCESSING",
                "message": "账单正在处理中，请稍后重试",
                "field": None,
            },
        )
    if batch.status == "failed":
        return HTTPException(
            409,
            detail={
                "code": "BATCH_FAILED",
                "message": "该批次处理失败，请重新确认账单",
                "field": None,
            },
        )
    return None


@router.post("/confirm")
async def confirm_bill(
    file: UploadFile = File(...),
    file_fingerprint: str = Form(...),
    mapping_json: str = Form(...),
    platform_scope: str = Form(...),
    remember_layout: bool = Form(...),
    current=Depends(require_current_db_role("admin")),
    db: AsyncSession = Depends(get_db),
    _assistant_intake_cycle_id: str | None = Depends(_direct_confirm_context),
    _assistant_intake_document_id: str | None = Depends(
        _direct_confirm_document_context
    ),
):
    caller_owned_transaction = _assistant_intake_cycle_id is not None
    data, fingerprint = await _read_bill_file(file)
    if fingerprint != file_fingerprint:
        raise _api_error("FILE_CHANGED", "确认的文件与分析结果不一致，请重新分析", 409, field="file")

    try:
        coordinates = MappingCoordinates.model_validate_json(mapping_json)
    except ValidationError:
        raise _api_error("MAPPING_INCOMPLETE", "字段映射无效", 422, field="mapping_json") from None
    try:
        scope = PlatformScope(platform_scope)
    except ValueError:
        raise _api_error("MAPPING_INCOMPLETE", "平台范围无效", 422, field="platform_scope") from None

    filename = file.filename or ""
    try:
        sheets, datemode = await asyncio.to_thread(load_workbook_rows, data, filename)
    except BillParseError:
        raise _api_error("FILE_INVALID", "无法解析该 Excel 文件", 422, field="file") from None
    signature = build_layout_signature(sheets)
    analysis = analyze_mapping(sheets, datemode, coordinates)
    error = _first_analysis_error(analysis)
    if error is not None:
        raise error
    # materialization happens before lifecycle persistence so a malformed confirmed
    # mapping can never create a rejected/processing batch.
    try:
        mapping = materialize_mapping(sheets, coordinates)
    except BillParseError:
        raise _api_error("MAPPING_OUT_OF_RANGE", "确认的汇总坐标无效", 422, field="summary_cell") from None

    source_key = source_namespace(scope, signature)
    rows = extract_bill_rows(sheets[mapping.sheet], mapping, datemode)
    bill_month = infer_bill_month(rows)
    try:
        await lock_recon_write_control(
            db,
            source_key=source_key,
            bill_month=bill_month,
            bill_nos=list(aggregate_orders(rows)),
            assistant_intake_cycle_id=_assistant_intake_cycle_id,
            assistant_intake_document_id=_assistant_intake_document_id,
        )
    except ReconWriteControlled as exc:
        raise _api_error(
            "OTA_ASSISTANT_CONTROLLED",
            f"{exc.billing_month} 的OTA调整由月结方案控制，请在月结中心生成并审批方案",
            409,
        ) from exc
    processing = await get_or_create_processing_batch(
        db,
        fingerprint=fingerprint,
        filename=filename,
        platform=source_key,
        user_id=current["user_id"],
        layout_signature=signature,
        confirmed_mapping=coordinates,
    )
    processing_batch = processing.batch
    if processing.state is ProcessingBatchState.in_flight:
        raise HTTPException(
            409,
            detail={
                "code": "PROCESSING",
                "message": "账单正在处理中，请稍后重试",
                "field": None,
            },
        )
    if processing.state is ProcessingBatchState.parsed:
        diffs = await _batch_diffs(db, processing_batch.batch_id)
        return {"batch": _batch_out(processing_batch), "diffs": [_diff_out(d) for d in diffs]}

    try:
        batch = await run_recon(
            db, data=data, filename=filename, user_id=current["user_id"], mapping=mapping,
            platform_scope=scope, source_key=source_key, layout_signature=signature,
            upload_fingerprint=fingerprint, processing_batch=processing_batch, enable_ai_enrichment=False,
            assistant_intake_cycle_id=_assistant_intake_cycle_id,
            assistant_intake_document_id=_assistant_intake_document_id,
            commit=not caller_owned_transaction,
        )
    except ReconWriteControlled as exc:
        await db.rollback()
        if processing.state is ProcessingBatchState.created:
            stale_processing = await db.get(ReconBatch, processing_batch.batch_id)
            if stale_processing is not None and stale_processing.status == "processing":
                await db.delete(stale_processing)
                await db.commit()
        raise _api_error(
            "OTA_ASSISTANT_CONTROLLED",
            f"{exc.billing_month} 的OTA调整由月结方案控制，请在月结中心生成并审批方案",
            409,
        ) from exc
    except BillRejected as exc:
        # The same deterministic gate was checked above.  Preserve a safe code if
        # a parser-level race/edge condition reaches the engine.
        raise _api_error("PARSE_QUALITY_LOW", "账单数据未通过本地校验", 422, field="mapping_json") from exc
    except Exception:
        await mark_processing_batch_failed(db, processing_batch.batch_id)
        raise

    # Direct billing-recon requests retain their historical durable boundary.  The
    # monthly-close adapter owns one transaction spanning recon, document lineage,
    # and its audit record, so internal assistant intake only flushes here.
    batch.mapping = {**(batch.mapping or {}), "warnings": analysis.warnings}
    if caller_owned_transaction:
        await db.flush()
    else:
        await db.commit()
    await db.refresh(batch)

    batch_id = batch.batch_id
    if remember_layout:
        try:
            await save_layout_template(
                db, signature=signature, coordinates=coordinates, platform_scope=scope,
                user_id=current["user_id"], sheet_names=list(sheets),
            )
            if caller_owned_transaction:
                await db.flush()
            else:
                await db.commit()
        except Exception:  # noqa: BLE001 - template retention cannot undo reconciliation
            if caller_owned_transaction:
                raise
            await db.rollback()
            logger.warning("billing-recon layout template save failed", exc_info=True)
            batch = await db.get(ReconBatch, batch_id)

    diffs = await _batch_diffs(db, batch_id)
    try:
        if caller_owned_transaction:
            return {"batch": _batch_out(batch), "diffs": [_diff_out(d) for d in diffs]}
        from app.services.feishu_lead_alert import send_billing_recon_alert

        counts = _diff_class_counts(diffs)
        counts_text = "、".join(f"{k}×{v}" for k, v in counts.items()) or "无"
        await send_billing_recon_alert(
            f"📒 账单对账 {batch.bill_month}：账单合计 ¥{batch.summary_total}，"
            f"共 {batch.row_count} 行，发现差异 {len(diffs)} 条（{counts_text}）。"
            f"批次 {batch.batch_id}，点击直接处理："
            f"{settings.FRONTEND_BASE_URL}/finance/billing-recon?batch={batch.batch_id}"
        )
    except Exception:  # noqa: BLE001 — 摘要失败不影响主流程
        logger.warning("billing-recon feishu summary failed", exc_info=True)

    return {"batch": _batch_out(batch), "diffs": [_diff_out(d) for d in diffs]}


@router.post("/upload")
async def upload_bill(current=Depends(require_current_db_role("admin"))):
    """Close the unsafe one-step execution bypass without consuming an upload body."""
    raise _api_error(
        "CONFIRMATION_REQUIRED", "请先分析账单并确认字段映射后再开始对账", 409,
    )


@router.get("/batches")
async def list_batches(current=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    rows = await db.execute(
        select(ReconBatch).where(
            ReconBatch.status.in_(("parsed", "rejected")),
        ).order_by(ReconBatch.created_at.desc()).limit(100)
    )
    visible = [b for b in rows.scalars().all() if not (b.mapping or {}).get("archived_at")]
    return [_batch_out(b) for b in visible[:50]]


@router.post("/batches/{batch_id}/archive")
async def archive_batch(
    batch_id: str,
    current=Depends(require_current_db_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    await _reject_assistant_controlled_write(db, batch_id)
    batch = (await db.execute(
        select(ReconBatch).where(ReconBatch.batch_id == batch_id).with_for_update()
    )).scalar_one_or_none()
    if batch is None:
        raise HTTPException(404, "批次不存在")
    state_error = _batch_state_error(batch)
    if state_error is not None:
        raise state_error
    mapping = dict(batch.mapping or {})
    if mapping.get("archived_at"):
        return {"archived_at": mapping["archived_at"]}
    if batch.status not in {"parsed", "rejected"}:
        raise _api_error("BATCH_NOT_ARCHIVABLE", "该批次当前不可归档", 409)
    archived_at = datetime.now(timezone.utc).isoformat()
    batch.mapping = {**mapping, "archived_at": archived_at, "archived_by": current["user_id"]}
    await log_action_tx(
        db, current["user_id"], "billing_recon.archive", "recon_batch", batch_id,
        before_data={"archived_at": None}, after_data={"archived_at": archived_at},
    )
    await db.commit()
    return {"archived_at": archived_at}


@router.get("/batches/{batch_id}")
async def batch_detail(batch_id: str, current=Depends(require_role("admin")), db: AsyncSession = Depends(get_db)):
    batch = await db.get(ReconBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "批次不存在")
    state_error = _batch_state_error(batch)
    if state_error is not None:
        raise state_error
    if (batch.mapping or {}).get("archived_at"):
        raise HTTPException(404, "批次不存在")
    diffs = await _batch_diffs(db, batch_id)
    batch_out = _batch_out(batch)
    batch_out["summary"] = build_live_summary(diffs)
    return {"batch": batch_out, "diffs": [_diff_out(d) for d in diffs]}


@router.post("/batches/{batch_id}/review")
async def review_recon_batch(
    batch_id: str,
    current=Depends(require_current_db_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    await _reject_assistant_controlled_write(db, batch_id)
    batch = (await db.execute(
        select(ReconBatch).where(ReconBatch.batch_id == batch_id).with_for_update()
    )).scalar_one_or_none()
    if batch is None:
        raise HTTPException(404, "批次不可用")
    state_error = _batch_state_error(batch)
    if state_error is not None:
        raise state_error
    if (batch.mapping or {}).get("archived_at"):
        raise HTTPException(404, "批次不可用")
    if batch.status != "parsed":
        raise _api_error("BATCH_NOT_REVIEWABLE", "该批次不可复核", 409)
    try:
        result = await review_batch(db, batch_id, current["user_id"])
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "批次不可用")
    return result


@router.post("/diffs/{diff_id}/action")
async def diff_action(
    diff_id: str,
    payload: dict,
    current=Depends(require_current_db_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    diff = await db.get(ReconDiff, diff_id)
    if diff is None:
        raise HTTPException(404, "差异不存在")
    action = str(payload.get("action", ""))
    delegated = await _delegate_assistant_decision(
        db,
        diff,
        current,
        {
            "decision": "action",
            "action": action,
            **({"reason": str(payload.get("reason", ""))} if action == "dismiss" else {}),
        },
        str(payload.get("request_id", "")).strip() or None,
    )
    if delegated is not None:
        return delegated
    try:
        return await apply_diff_action(db, diff, action, current["user_id"])
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/diffs/{diff_id}/claim")
async def claim_diff(
    diff_id: str,
    payload: dict,
    current=Depends(require_current_db_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    diff = await db.get(ReconDiff, diff_id)
    if diff is None:
        raise HTTPException(404, "差异不存在")
    order_id = str(payload.get("order_id", ""))
    if not order_id:
        raise HTTPException(400, "缺少 order_id")
    delegated = await _delegate_assistant_decision(
        db,
        diff,
        current,
        {"decision": "claim", "order_id": order_id},
        str(payload.get("request_id", "")).strip() or None,
    )
    if delegated is not None:
        return delegated
    try:
        return await claim_match(db, diff, order_id, current["user_id"])
    except ValueError as e:
        raise HTTPException(400, str(e))
