"""Actions that run existing reconciliation engines from archived close documents."""

from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models.monthly_close import MonthlyCloseCycle, MonthlyCloseDocument
from app.models.utility_recon import UtilityReconBatch
from app.services.audit import log_action_tx
from app.services.monthly_close.documents import (
    MonthlyCloseDocumentError,
    reprocess_active_service_documents,
)
from app.services.utility_recon.contracts import WorkbookInput
from app.services.utility_recon.run import run_upload
from app.services.utility_recon.ai_mapping import UtilityColumnMapping
from app.services.utility_recon.workbook import inspect_workbooks_with_ai
from app.models.owner import Owner
from app.services.service_fee_reconciliation import (
    ServiceFeeReconciliationError,
    apply_service_fee_reconciliation,
    plan_service_fee_reconciliation,
)


async def run_utility_from_documents(
    db: AsyncSession, cycle: MonthlyCloseCycle, user_id: str
):
    documents = list(
        (
            await db.execute(
                select(MonthlyCloseDocument)
                .options(undefer(MonthlyCloseDocument.content))
                .where(
                    MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                    MonthlyCloseDocument.source_type.in_(
                        {"utility_receipt", "utility_expense"}
                    ),
                    MonthlyCloseDocument.is_active.is_(True),
                )
                .order_by(MonthlyCloseDocument.uploaded_at)
            )
        ).scalars()
    )
    by_source: dict[str, list[MonthlyCloseDocument]] = {}
    for document in documents:
        by_source.setdefault(document.source_type, []).append(document)
    for source_type in ("utility_receipt", "utility_expense"):
        count = len(by_source.get(source_type, []))
        if count != 1:
            raise MonthlyCloseDocumentError(
                "utility_document_count_invalid",
                "水电对账必须各保留一份有效的已收明细和费用明细；请先归档多余文件。",
                409,
            )

    linked_batch_ids = {
        document.engine_id
        for document in documents
        if document.engine_type == "utility_recon" and document.engine_id
    }
    if len(linked_batch_ids) == 1 and all(
        document.engine_type == "utility_recon" and document.engine_id
        for document in documents
    ):
        existing_batch = await db.get(UtilityReconBatch, next(iter(linked_batch_ids)))
        if existing_batch is not None and existing_batch.month == cycle.billing_month:
            return existing_batch

    files = [
        WorkbookInput(document.filename, document.content)
        for document in documents
    ]
    mappings: list[UtilityColumnMapping | None] = []
    for document in documents:
        stored = (document.metadata_ or {}).get("utility_mapping")
        remembered = None
        if not isinstance(stored, dict):
            from app.services.monthly_close.layout_memory import find_remembered_mapping

            remembered = await find_remembered_mapping(
                db,
                source_type=document.source_type,
                data=document.content,
                filename=document.filename,
            )
        candidate = stored if isinstance(stored, dict) else remembered
        mappings.append(
            UtilityColumnMapping.model_validate(candidate)
            if candidate is not None
            else None
        )
    preflight = await inspect_workbooks_with_ai(
        files,
        mappings=mappings,
        target_month=cycle.billing_month,
    )
    expected_roles = {
        "utility_receipt": "receipt",
        "utility_expense": "expense",
    }
    for document, inspected in zip(documents, preflight.files, strict=True):
        if inspected.role != expected_roles[document.source_type]:
            raise MonthlyCloseDocumentError(
                "utility_role_mismatch",
                "水电资料类型与表格内容不一致，请检查分类。",
                422,
            )
    batches = await run_upload(db, files, user_id, preflight=preflight)
    batch = next(
        (item for item in batches if item.month == cycle.billing_month), None
    )
    if batch is None:
        raise MonthlyCloseDocumentError(
            "utility_month_not_found",
            f"两份水电资料中没有共同的 {cycle.billing_month} 数据。",
            422,
        )
    for document, inspected in zip(documents, preflight.files, strict=True):
        table = inspected.sheets[0]
        mapping = UtilityColumnMapping(
            role=inspected.role,
            sheet=table.sheet,
            header_row=table.header_row - 1,
            columns=table.columns,
        )
        document.engine_type = "utility_recon"
        document.engine_id = batch.batch_id
        document.processing_status = "processed"
        document.processing_error = None
        document.metadata_ = {
            **(document.metadata_ or {}),
            "utility_batch_id": batch.batch_id,
            "billing_month": cycle.billing_month,
            "utility_mapping": mapping.model_dump(mode="json"),
        }
        from app.services.monthly_close.layout_memory import remember_document_mapping

        remember_document_mapping(
            document,
            data=document.content,
            filename=document.filename,
            mapping=mapping,
        )
    await log_action_tx(
        db,
        user_id,
        "monthly_close.utility.run",
        "monthly_close",
        cycle.cycle_id,
        after_data={
            "billing_month": cycle.billing_month,
            "batch_id": batch.batch_id,
            "document_ids": sorted(document.document_id for document in documents),
        },
    )
    await db.commit()
    return batch


async def reconcile_service_fees_from_documents(
    db: AsyncSession, cycle: MonthlyCloseCycle, user_id: str
) -> dict:
    year, month = (int(part) for part in cycle.billing_month.split("-"))
    owner_ids = list(
        (await db.execute(select(Owner.owner_id).order_by(Owner.owner_id))).scalars()
    )
    created_count = 0
    corrected_count = 0
    blocked: list[dict] = []
    for owner_id in owner_ids:
        plan = await plan_service_fee_reconciliation(db, owner_id, year, month)
        if plan.unresolved:
            blocked.extend(
                {
                    "owner_id": owner_id,
                    "reason": item.reason,
                    "order_id": item.order_id,
                    "room_id": item.room_id,
                }
                for item in plan.unresolved
            )
            continue
        try:
            result = await apply_service_fee_reconciliation(
                db, plan, operator_id=user_id
            )
        except ServiceFeeReconciliationError as exc:
            blocked.extend(
                {
                    "owner_id": owner_id,
                    "reason": item.reason,
                    "order_id": item.order_id,
                    "room_id": item.room_id,
                }
                for item in exc.unresolved
            )
            continue
        created_count += result.created_count
        corrected_count += result.corrected_count
    documents = list(
        (
            await db.execute(
                select(MonthlyCloseDocument)
                .options(undefer(MonthlyCloseDocument.content))
                .where(
                    MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                    MonthlyCloseDocument.source_type.in_(
                        {"cleaning_statement", "linen_statement"}
                    ),
                    MonthlyCloseDocument.is_active.is_(True),
                )
            )
        ).scalars()
    )
    await reprocess_active_service_documents(
        db, cycle.cycle_id, billing_month=cycle.billing_month
    )
    await log_action_tx(
        db,
        user_id,
        "monthly_close.service_fees.reconcile",
        "monthly_close",
        cycle.cycle_id,
        after_data={
            "billing_month": cycle.billing_month,
            "created_count": created_count,
            "corrected_count": corrected_count,
            "blocked_count": len(blocked),
            "document_count": len(documents),
        },
    )
    await db.commit()
    return {
        "created_count": created_count,
        "corrected_count": corrected_count,
        "blocked": blocked,
        "reprocessed_document_count": len(documents),
    }
