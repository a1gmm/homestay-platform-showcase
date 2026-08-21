"""水电对账上传的原子事务编排。"""

from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.utility_recon import UtilityReconBatch, UtilityReconRow, UtilityReconSuggestion, UtilityReconUpload
from .contracts import WorkbookInput
from .matcher import reconcile_month
from .normalize import normalize_table
from .workbook import inspect_workbooks_with_ai


def _id(prefix: str) -> str:
    return f"{prefix}-{uuid4().hex[:24]}"


def _summary_json(summary) -> dict:
    return {
        "receipt_total": str(summary.receipt_total),
        "expense_total": str(summary.expense_total),
        "total_difference": str(summary.total_difference),
        "by_floor_category": [
            {"floor": floor, "category": category, "receipt": str(item.receipt), "expense": str(item.expense), "difference": str(item.difference)}
            for (floor, category), item in sorted(summary.by_floor_category.items())
        ],
    }


async def run_upload(db: AsyncSession, files: list[WorkbookInput], actor_id: str) -> list[UtilityReconBatch]:
    preflight = await inspect_workbooks_with_ai(files)
    by_filename = {item.filename: item for item in files}
    fingerprints = {
        inspected.role: sha256(by_filename[inspected.filename].content).hexdigest()
        for inspected in preflight.files
    }
    fingerprint_key = sha256(f"{fingerprints['receipt']}:{fingerprints['expense']}".encode()).hexdigest()
    existing = (await db.execute(
        select(UtilityReconUpload).where(UtilityReconUpload.file_fingerprints["pair"].as_string() == fingerprint_key)
    )).scalar_one_or_none()
    if existing:
        result = await db.execute(select(UtilityReconBatch).where(UtilityReconBatch.upload_id == existing.upload_id))
        return list(result.scalars().all())

    normalized = []
    excluded_count = 0
    unparseable_count = 0
    for inspected in preflight.files:
        for table in inspected.sheets:
            result = normalize_table(table)
            normalized.extend(result.valid + result.excluded + result.unparseable)
            excluded_count += len(result.excluded)
            unparseable_count += len(result.unparseable)
    upload = UtilityReconUpload(
        upload_id=_id("URU"),
        receipt_filename=next(item.filename for item in preflight.files if item.role == "receipt"),
        expense_filename=next(item.filename for item in preflight.files if item.role == "expense"),
        file_fingerprints={**fingerprints, "pair": fingerprint_key},
        role_mapping={item.filename: item.role for item in preflight.files},
        receipt_months=next(item.months for item in preflight.files if item.role == "receipt"),
        expense_months=next(item.months for item in preflight.files if item.role == "expense"),
        common_months=preflight.common_months,
        preflight_stats={"excluded": excluded_count, "unparseable": unparseable_count},
        status="completed",
        created_by=actor_id,
    )
    db.add(upload)
    batches: list[UtilityReconBatch] = []
    for month in preflight.common_months:
        month_rows = [row for row in normalized if row.month == month]
        result = reconcile_month(month, month_rows)
        batch = UtilityReconBatch(
            batch_id=_id("URB"), upload_id=upload.upload_id, month=month,
            raw_receipt_total=result.raw.receipt_total, raw_expense_total=result.raw.expense_total,
            raw_difference=result.raw.total_difference, corrected_difference=result.raw.total_difference,
            raw_summary=_summary_json(result.raw), corrected_summary=_summary_json(result.raw),
            anomaly_counts={kind: sum(item.kind == kind for item in result.suggestions) for kind in {item.kind for item in result.suggestions}},
        )
        db.add(batch)
        batches.append(batch)
        row_id_map: dict[str, str] = {}
        for row in month_rows:
            stored_row_id = _id("URR")
            row_id_map[row.row_id] = stored_row_id
            db.add(UtilityReconRow(
                row_id=stored_row_id, batch_id=batch.batch_id, side=row.side,
                business_date=row.business_date, month=month, floor=row.floor, room=row.room,
                category=row.category, amount=row.amount, source_filename=row.source_filename,
                source_sheet=row.source_sheet, source_row_number=row.source_row_number,
                raw_values={key: str(value) if value is not None else None for key, value in row.raw_values.items()},
                customer_name=row.customer_name, normalization_warnings=list(row.warnings),
                disposition=row.disposition, exclusion_reason=row.exclusion_reason,
            ))
        for item in result.suggestions:
            stored_patch = dict(item.patch)
            if "row_id" in stored_patch:
                stored_patch["row_id"] = row_id_map[stored_patch["row_id"]]
            db.add(UtilityReconSuggestion(
                suggestion_id=_id("URS"), batch_id=batch.batch_id, kind=item.kind,
                related_row_ids=[row_id_map[row_id] for row_id in item.related_row_ids], patch=stored_patch,
                evidence=item.evidence, confidence=item.confidence, impact={}, status="pending",
            ))
    await db.commit()
    for batch in batches:
        await db.refresh(batch)
    return batches
