"""Administrator-only API for the fixed nine-step monthly close."""

from __future__ import annotations

from datetime import datetime, timezone
from io import BytesIO
import json
from typing import Literal
from urllib.parse import quote

import openai
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from fastapi.responses import Response
from starlette.datastructures import Headers
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import RedisClient, get_db, require_role
from app.core.rate_limit import enforce_rate_limit
from app.models.monthly_close import MonthlyCloseCycle
from app.models.room import Room
from app.services.monthly_close.workflow import (
    MonthlyCloseConflict,
    build_lightweight_cycle_summaries,
    build_cycle_summary,
    build_cycle_view,
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
    analyze_operating_expense_statement,
)
from app.services.utility_recon.ai_mapping import UtilityColumnMapping
from app.services.utility_recon.contracts import WorkbookInput
from app.services.utility_recon.workbook import (
    WorkbookInspectionError,
    inspect_workbook_with_ai,
)
from app.api.v1.billing_recon import analyze_bill, confirm_bill
from app.models.monthly_close import MonthlyCloseDocument
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
    receive_inbox_item,
    resolve_intake_link,
    revoke_intake_link,
    set_inbox_source,
)


router = APIRouter(prefix="/monthly-close", tags=["monthly-close"])


class ConfirmStepBody(BaseModel):
    expected_evidence_hash: str = Field(min_length=64, max_length=64)
    note: str | None = Field(default=None, max_length=1000)


class ReopenBody(BaseModel):
    reason: str = Field(min_length=1, max_length=1000)


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
    source_type: str | None = None


class LayoutMemoryEnabledBody(BaseModel):
    enabled: bool


class UtilityAnalyzeBody(BaseModel):
    mapping: UtilityColumnMapping | None = None


class UtilityConfirmBody(BaseModel):
    mapping: UtilityColumnMapping


class UtilityAnalysis(BaseModel):
    mapping: UtilityColumnMapping
    months: list[str]
    suggested_by: Literal["deterministic", "ai", "remembered", "administrator"]
    needs_confirmation: bool = True
    sheets: list[dict] = Field(default_factory=list)


def _http_conflict(exc: MonthlyCloseConflict) -> HTTPException:
    status_code = 422 if exc.code == "invalid_billing_month" else 409
    return HTTPException(status_code=status_code, detail=exc.to_detail())


def _document_error(exc: MonthlyCloseDocumentError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.to_detail())


def _inbox_error(exc: MonthlyCloseInboxError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.to_detail())


SOURCE_LABELS = {
    "cleaning_statement": "保洁供应商对账单",
    "linen_statement": "布草供应商对账单",
    "utility_receipt": "水电已收明细",
    "utility_expense": "水电费用明细",
    "ota_statement": "OTA平台账单",
    "operating_expenses": "运营支出明细",
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


def _upload_from_document(document: MonthlyCloseDocument) -> UploadFile:
    return UploadFile(
        BytesIO(document.content),
        size=document.byte_size,
        filename=document.filename,
        headers=Headers({"content-type": document.mime_type}),
    )


@router.get("")
async def list_monthly_closes(
    current=Depends(require_role("admin")),
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
    current=Depends(require_role("admin")),
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
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    return await list_layout_memories(db)


@router.get("/layout-memory-metrics")
async def get_monthly_close_layout_memory_metrics(
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    return await layout_memory_metrics(db)


@router.patch("/layout-memories/{document_id}")
async def update_monthly_close_layout_memory(
    document_id: str,
    body: LayoutMemoryEnabledBody,
    current=Depends(require_role("admin")),
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
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_or_create_cycle(db, billing_month, current["user_id"])
        return await build_cycle_view(db, cycle)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.get("/{billing_month}")
async def get_monthly_close(
    billing_month: str,
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    if cycle is None:
        raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
    return await build_cycle_view(db, cycle)


@router.post("/{billing_month}/steps/{step_key}/confirm")
async def confirm_monthly_close_step(
    billing_month: str,
    step_key: str,
    body: ConfirmStepBody,
    current=Depends(require_role("admin")),
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
    current=Depends(require_role("admin")),
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
    file: UploadFile = File(...),
    current=Depends(require_role("admin")),
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
            source_type=source_type,
            filename=file.filename or "document.xlsx",
            mime_type=file.content_type or "application/octet-stream",
            data=data,
            user_id=current["user_id"],
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


@router.post(
    "/{billing_month}/documents/classify",
    response_model=SourceClassification,
)
async def classify_monthly_close_document(
    billing_month: str,
    file: UploadFile = File(...),
    current=Depends(require_role("admin")),
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
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(
                404,
                detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"},
            )
        return [inbox_item_view(item) for item in await list_inbox_items(db, cycle.cycle_id)]
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post("/{billing_month}/inbox")
async def receive_monthly_close_inbox_file(
    billing_month: str,
    file: UploadFile = File(...),
    current=Depends(require_role("admin")),
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
        item = await receive_inbox_item(
            db,
            cycle,
            filename=file.filename or "document.xlsx",
            mime_type=file.content_type or "application/octet-stream",
            data=await file.read(),
            origin="admin",
            user_id=current["user_id"],
        )
        return inbox_item_view(item)
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
    current=Depends(require_role("admin")),
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
    current=Depends(require_role("admin")),
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
    current=Depends(require_role("admin")),
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


@router.get("/{billing_month}/intake-links")
async def get_monthly_close_intake_links(
    billing_month: str,
    current=Depends(require_role("admin")),
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
    current=Depends(require_role("admin")),
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
    current=Depends(require_role("admin")),
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
    db: AsyncSession = Depends(get_db),
):
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
        link, cycle = await resolve_intake_link(db, token)
        await enforce_rate_limit(
            redis,
            f"monthly_close_intake:{link.link_id}:{client_ip}",
            limit=30,
            window_seconds=3600,
        )
        if file.size and file.size > MAX_MONTHLY_CLOSE_DOCUMENT_BYTES:
            raise MonthlyCloseDocumentError("document_too_large", "文件超过 10MB", 413)
        item = await receive_inbox_item(
            db,
            cycle,
            filename=file.filename or "document.xlsx",
            mime_type=file.content_type or "application/octet-stream",
            data=await file.read(),
            origin="external",
            user_id=None,
            source_type=link.source_type,
            submitted_label=link.label,
        )
        link.last_uploaded_at = datetime.now(timezone.utc)
        await db.commit()
        return {
            "receipt_id": item.item_id,
            "status": item.status,
            "message": "文件已收到，管理员确认后会进入本月对账。",
        }
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
    current=Depends(require_role("admin")),
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


@router.post("/{billing_month}/documents/{document_id}/archive")
async def archive_monthly_close_document(
    billing_month: str,
    document_id: str,
    current=Depends(require_role("admin")),
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
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
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
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        _, document = await _cycle_document(db, billing_month, document_id)
        if document.source_type not in {"cleaning_statement", "linen_statement"}:
            raise HTTPException(
                422,
                detail={"code": "not_service_statement", "message": "该文件不是保洁或布草对账单"},
            )
        return await analyze_service_statement(
            document.content,
            document.filename,
            document.source_type,
            mapping=body.mapping,
            billing_month=billing_month,
        )
    except ServiceStatementError as exc:
        raise _service_statement_error(exc) from exc
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except (openai.APIStatusError, openai.APIConnectionError, ServiceMappingError) as exc:
        raise HTTPException(
            503,
            detail={"code": "service_mapping_unavailable", "message": "表格结构识别暂不可用，可重试或人工填写列号"},
        ) from exc


@router.post("/{billing_month}/documents/{document_id}/service/confirm")
async def confirm_monthly_close_service_document(
    billing_month: str,
    document_id: str,
    body: ServiceConfirmBody,
    current=Depends(require_role("admin")),
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
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        _, document = await _cycle_document(
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
        return await analyze_operating_expense_statement(
            document.content,
            document.filename,
            billing_month,
            valid_room_ids=valid_room_ids,
            room_aliases=build_room_aliases(room_rows),
            mapping=confirmed_mapping,
            mapping_origin=mapping_origin,
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
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle, document = await _cycle_document(
            db,
            billing_month,
            document_id,
            source_type="operating_expenses",
        )
        cycle = await require_cycle_writable(db, cycle)
        return await import_operating_expense_document(
            db,
            cycle,
            document,
            current["user_id"],
            mapping=body.mapping.to_expense_mapping(),
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
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
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        _, document = await _cycle_document(db, billing_month, document_id)
        expected_role = {
            "utility_receipt": "receipt",
            "utility_expense": "expense",
        }.get(document.source_type)
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
        return UtilityAnalysis(
            mapping=coordinates,
            months=inspected.months,
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
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle, document = await _cycle_document(db, billing_month, document_id)
        await require_cycle_writable(db, cycle)
        expected_role = {
            "utility_receipt": "receipt",
            "utility_expense": "expense",
        }.get(document.source_type)
        if expected_role is None or body.mapping.role != expected_role:
            raise WorkbookInspectionError("资料类型与确认的流水角色不一致")
        inspected = await inspect_workbook_with_ai(
            WorkbookInput(document.filename, document.content),
            body.mapping,
            target_month=billing_month,
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
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
        cycle = await require_cycle_writable(db, cycle)
        batch = await run_utility_from_documents(db, cycle, current["user_id"])
        return {
            "batch_id": batch.batch_id,
            "month": batch.month,
            "status": batch.status,
        }
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
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
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
        cycle = await require_cycle_writable(db, cycle)
        return await reconcile_service_fees_from_documents(
            db, cycle, current["user_id"]
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post("/{billing_month}/documents/{document_id}/ota/analyze")
async def analyze_monthly_close_ota_document(
    billing_month: str,
    document_id: str,
    body: OtaAnalyzeBody,
    current=Depends(require_role("admin")),
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
            current=current,
            db=db,
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc


@router.post("/{billing_month}/actions/operating-expenses/import")
async def import_monthly_close_operating_expenses(
    billing_month: str,
    current=Depends(require_role("admin")),
    db: AsyncSession = Depends(get_db),
):
    try:
        cycle = await get_cycle_by_month(db, billing_month)
        if cycle is None:
            raise HTTPException(404, detail={"code": "monthly_close_not_found", "message": "该月尚未开始月结"})
        cycle = await require_cycle_writable(db, cycle)
        return await import_operating_expense_documents(
            db, cycle, current["user_id"]
        )
    except MonthlyCloseConflict as exc:
        raise _http_conflict(exc) from exc
    except MonthlyCloseDocumentError as exc:
        await db.rollback()
        raise _document_error(exc) from exc


@router.post("/{billing_month}/documents/{document_id}/ota/confirm")
async def confirm_monthly_close_ota_document(
    billing_month: str,
    document_id: str,
    body: OtaConfirmBody,
    current=Depends(require_role("admin")),
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
