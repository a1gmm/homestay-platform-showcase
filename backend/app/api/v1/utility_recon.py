"""水电费 Excel 自动对账 API。"""

from datetime import datetime, timezone
from io import BytesIO

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.deps import get_db, require_role
from app.models.utility_recon import UtilityReconBatch, UtilityReconRow, UtilityReconSuggestion
from app.services.audit import log_action_tx
from app.services.utility_recon.contracts import NormalizedRow, WorkbookInput
from app.services.utility_recon.export import build_export
from app.services.utility_recon.matcher import ReconciliationResult, apply_patches
from app.services.utility_recon.run import run_upload
from app.services.utility_recon.run import _summary_json
from app.services.utility_recon.summary import build_summary
from app.services.utility_recon.workbook import WorkbookInspectionError, inspect_workbooks_with_ai


router = APIRouter(prefix="/utility-recon", tags=["utility-recon"])


def _preflight_out(result) -> dict:
    return {
        "files": [
            {
                "filename": item.filename,
                "role": item.role,
                "months": item.months,
                "mapping_status": item.mapping_status,
                "sheets": [
                    {"name": sheet.sheet, "header_row": sheet.header_row, "row_count": len(sheet.rows), "months": sheet.months}
                    for sheet in item.sheets
                ],
            }
            for item in result.files
        ],
        "common_months": result.common_months,
        "receipt_only_months": result.receipt_only_months,
        "expense_only_months": result.expense_only_months,
    }


@router.post("/preflight")
async def preflight_utility_workbooks(
    files: list[UploadFile] = File(...),
    current=Depends(require_role("admin", "finance", "operator")),
):
    if len(files) != 2:
        raise HTTPException(422, "必须同时上传两份 Excel 文件")
    inputs: list[WorkbookInput] = []
    for file in files:
        if file.size and file.size > 10 * 1024 * 1024:
            raise HTTPException(413, "文件超过 10MB")
        content = await file.read()
        if len(content) > 10 * 1024 * 1024:
            raise HTTPException(413, "文件超过 10MB")
        inputs.append(WorkbookInput(file.filename or "", content))
    try:
        return _preflight_out(await inspect_workbooks_with_ai(inputs))
    except WorkbookInspectionError as exc:
        raise HTTPException(422, str(exc)) from exc


async def _read_inputs(files: list[UploadFile]) -> list[WorkbookInput]:
    if len(files) != 2:
        raise HTTPException(422, "必须同时上传两份 Excel 文件")
    result = []
    for file in files:
        content = await file.read()
        if (file.size and file.size > 10 * 1024 * 1024) or len(content) > 10 * 1024 * 1024:
            raise HTTPException(413, "文件超过 10MB")
        result.append(WorkbookInput(file.filename or "", content))
    return result


def _batch_out(batch: UtilityReconBatch) -> dict:
    return {
        "batch_id": batch.batch_id, "upload_id": batch.upload_id, "month": batch.month,
        "status": batch.status, "raw_receipt_total": str(batch.raw_receipt_total),
        "raw_expense_total": str(batch.raw_expense_total), "raw_difference": str(batch.raw_difference),
        "corrected_difference": str(batch.corrected_difference), "raw_summary": batch.raw_summary,
        "corrected_summary": batch.corrected_summary, "anomaly_counts": batch.anomaly_counts,
        "created_at": batch.created_at.isoformat() if batch.created_at else None,
    }


async def _refresh_corrected_summary(db: AsyncSession, batch: UtilityReconBatch) -> None:
    row_result = await db.execute(select(UtilityReconRow).where(UtilityReconRow.batch_id == batch.batch_id))
    rows = [
        NormalizedRow(
            row_id=row.row_id, side=row.side, business_date=row.business_date, month=row.month,
            floor=row.floor, room=row.room, category=row.category, amount=row.amount,
            source_filename=row.source_filename, source_sheet=row.source_sheet,
            source_row_number=row.source_row_number, raw_values=row.raw_values,
            customer_name=row.customer_name, warnings=tuple(row.normalization_warnings or []),
            disposition=row.disposition, exclusion_reason=row.exclusion_reason,
        )
        for row in row_result.scalars().all()
    ]
    suggestion_result = await db.execute(select(UtilityReconSuggestion).where(
        UtilityReconSuggestion.batch_id == batch.batch_id,
        UtilityReconSuggestion.status == "adopted",
    ))
    patches = [item.patch for item in suggestion_result.scalars().all() if item.patch]
    raw = build_summary(rows)
    corrected = apply_patches(ReconciliationResult(batch.month, rows, raw, []), patches)
    batch.corrected_summary = _summary_json(corrected)
    batch.corrected_difference = corrected.total_difference


@router.post("/run")
async def run_utility_reconciliation(
    files: list[UploadFile] = File(...), current=Depends(require_role("admin", "finance", "operator")),
    db: AsyncSession = Depends(get_db),
):
    try:
        batches = await run_upload(db, await _read_inputs(files), current["user_id"])
        return {"batches": [_batch_out(batch) for batch in batches]}
    except WorkbookInspectionError as exc:
        await db.rollback()
        raise HTTPException(422, str(exc)) from exc


@router.get("/batches/{batch_id}")
async def utility_batch_detail(
    batch_id: str, current=Depends(require_role("admin", "finance", "operator")),
    db: AsyncSession = Depends(get_db),
):
    batch = await db.get(UtilityReconBatch, batch_id)
    if batch is None:
        raise HTTPException(404, "对账批次不存在或无权访问")
    row_result = await db.execute(select(UtilityReconRow).where(UtilityReconRow.batch_id == batch_id).order_by(UtilityReconRow.source_filename, UtilityReconRow.source_sheet, UtilityReconRow.source_row_number))
    suggestion_result = await db.execute(select(UtilityReconSuggestion).where(UtilityReconSuggestion.batch_id == batch_id))
    rows = []
    for row in row_result.scalars().all():
        rows.append({
            "row_id": row.row_id, "side": row.side, "business_date": row.business_date.isoformat() if row.business_date else None,
            "floor": row.floor, "room": row.room, "category": row.category,
            "amount": str(row.amount) if row.amount is not None else None,
            "source_filename": row.source_filename, "source_sheet": row.source_sheet,
            "source_row_number": row.source_row_number,
            "customer_name": row.customer_name if current["role"] in {"admin", "finance"} else ("客户" if row.customer_name else None),
            "warnings": row.normalization_warnings, "disposition": row.disposition,
            "exclusion_reason": row.exclusion_reason,
        })
    suggestions = [{
        "suggestion_id": item.suggestion_id, "kind": item.kind, "related_row_ids": item.related_row_ids,
        "patch": item.patch, "evidence": item.evidence, "confidence": item.confidence,
        "impact": item.impact, "status": item.status,
    } for item in suggestion_result.scalars().all()]
    return {"batch": _batch_out(batch), "rows": rows, "suggestions": suggestions}


@router.get("/batches")
async def list_utility_batches(
    current=Depends(require_role("admin", "finance", "operator")), db: AsyncSession = Depends(get_db),
):
    result = await db.execute(select(UtilityReconBatch).order_by(UtilityReconBatch.created_at.desc()).limit(100))
    return [_batch_out(batch) for batch in result.scalars().all()]


@router.post("/suggestions/{suggestion_id}/{action}")
async def decide_utility_suggestion(
    suggestion_id: str, action: str, current=Depends(require_role("admin", "finance")),
    db: AsyncSession = Depends(get_db),
):
    if action not in {"adopt", "revert"}:
        raise HTTPException(404, "建议不存在或无权访问")
    suggestion = (await db.execute(
        select(UtilityReconSuggestion).where(UtilityReconSuggestion.suggestion_id == suggestion_id).with_for_update()
    )).scalar_one_or_none()
    if suggestion is None:
        raise HTTPException(404, "建议不存在或无权访问")
    batch = (await db.execute(
        select(UtilityReconBatch).where(UtilityReconBatch.batch_id == suggestion.batch_id).with_for_update()
    )).scalar_one()
    if batch.status == "closed":
        raise HTTPException(409, "批次已关闭")
    target = "adopted" if action == "adopt" else "reverted"
    if suggestion.status != target:
        before = suggestion.status
        suggestion.status = target
        suggestion.decided_by = current["user_id"]
        suggestion.decided_at = datetime.now(timezone.utc)
        await log_action_tx(db, current["user_id"], f"utility_recon.suggestion.{action}", "utility_recon_suggestion", suggestion_id, {"status": before}, {"status": target})
        batch.version += 1
        await db.flush()
        await _refresh_corrected_summary(db, batch)
        await db.commit()
    return {"suggestion_id": suggestion_id, "status": suggestion.status}


@router.post("/batches/{batch_id}/close")
async def close_utility_batch(
    batch_id: str, current=Depends(require_role("admin", "finance")), db: AsyncSession = Depends(get_db),
):
    batch = (await db.execute(select(UtilityReconBatch).where(UtilityReconBatch.batch_id == batch_id).with_for_update())).scalar_one_or_none()
    if batch is None:
        raise HTTPException(404, "对账批次不存在或无权访问")
    if batch.status != "closed":
        batch.status = "closed"
        batch.closed_by = current["user_id"]
        batch.closed_at = datetime.now(timezone.utc)
        await log_action_tx(db, current["user_id"], "utility_recon.batch.close", "utility_recon_batch", batch_id, {"status": "open"}, {"status": "closed"})
        await db.commit()
    return {"batch_id": batch_id, "status": "closed"}


@router.get("/batches/{batch_id}/export")
async def export_utility_batch(
    batch_id: str, current=Depends(require_role("admin", "finance")), db: AsyncSession = Depends(get_db),
):
    detail = await utility_batch_detail(batch_id, current, db)
    data = build_export(detail)
    filename = f"水电费对账结果_{detail['batch']['month']}.xlsx"
    return StreamingResponse(
        BytesIO(data), media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{filename}"},
    )
