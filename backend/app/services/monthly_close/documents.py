"""Private document lifecycle for one monthly close cycle."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models.expense import Expense
from app.models.monthly_close import (
    MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES,
    MONTHLY_CLOSE_SOURCE_TYPES,
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseInboxItem,
    MonthlyCloseServiceLine,
    MonthlyCloseSourceRequirement,
    uses_legacy_utility_contract,
)
from app.services.audit import log_action_tx
from app.services.billing_recon.parser import BillParseError
from app.services.billing_recon.upload import upload_fingerprint, validate_bill_container


MAX_MONTHLY_CLOSE_DOCUMENT_BYTES = 10 * 1024 * 1024


class MonthlyCloseDocumentError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)

    def to_detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message}


def _derived_expense_ids(document: MonthlyCloseDocument) -> list[str]:
    metadata = document.metadata_ or {}
    values = metadata.get("import_result", {}).get("expense_ids", [])
    values = values if isinstance(values, list) else []
    event_links = metadata.get("operating_expense_event_links", {})
    linked_values = (
        [item.get("expense_id") for item in event_links.values()]
        if isinstance(event_links, dict)
        else []
    )
    return sorted(
        {
            value
            for value in [*values, *linked_values]
            if isinstance(value, str) and value.startswith("EXM-")
        }
    )


async def _set_derived_expenses_active(
    db: AsyncSession,
    document: MonthlyCloseDocument,
    *,
    active: bool,
    user_id: str,
    changed_at: datetime,
) -> list[str]:
    """Soft-void or restore only expenses deterministically derived from a file."""
    expense_ids = _derived_expense_ids(document)
    if not expense_ids:
        return []
    if not active:
        active_others = list(
            (
                await db.execute(
                    select(MonthlyCloseDocument).where(
                        MonthlyCloseDocument.cycle_id == document.cycle_id,
                        MonthlyCloseDocument.source_type == document.source_type,
                        MonthlyCloseDocument.is_active.is_(True),
                        MonthlyCloseDocument.document_id != document.document_id,
                    )
                )
            ).scalars()
        )
        still_referenced = {
            expense_id
            for other in active_others
            for expense_id in _derived_expense_ids(other)
        }
        expense_ids = sorted(set(expense_ids).difference(still_referenced))
        if not expense_ids:
            return []
    expenses = list(
        (
            await db.execute(
                select(Expense)
                .where(Expense.expense_id.in_(expense_ids))
                .with_for_update()
            )
        ).scalars()
    )
    for expense in expenses:
        expense.is_deleted = not active
        expense.deleted_at = None if active else changed_at
        expense.deleted_by = None if active else user_id
    return sorted(expense.expense_id for expense in expenses)


async def _ensure_operating_expense_reactivation_is_unique(
    db: AsyncSession, document: MonthlyCloseDocument
) -> None:
    own_keys = set((document.metadata_ or {}).get("expense_business_keys", []))
    active_others = list(
        (
            await db.execute(
                select(MonthlyCloseDocument).where(
                    MonthlyCloseDocument.cycle_id == document.cycle_id,
                    MonthlyCloseDocument.source_type == "operating_expenses",
                    MonthlyCloseDocument.is_active.is_(True),
                    MonthlyCloseDocument.document_id != document.document_id,
                )
            )
        ).scalars()
    )
    active_other_ids = {other.document_id for other in active_others}
    superseded_by = set(
        (document.metadata_ or {}).get("superseded_by_document_ids", [])
    )
    supersedes = set(
        (document.metadata_ or {}).get("supersedes_document_ids", [])
    )
    explicitly_replaced = (
        bool(active_other_ids.intersection(superseded_by))
        or bool(active_other_ids.intersection(supersedes))
        or any(
            document.document_id
            in set((other.metadata_ or {}).get("supersedes_document_ids", []))
            for other in active_others
        )
    )
    conflicts = sorted(
        own_keys.intersection(
            key
            for other in active_others
            for key in (other.metadata_ or {}).get("expense_business_keys", [])
        )
    )
    if explicitly_replaced or conflicts:
        raise MonthlyCloseDocumentError(
            "operating_expense_reactivation_conflict",
            "该原件中的支出已由其他有效文件替代，请先归档替代文件。",
        )


def validate_source_type(source_type: str) -> str:
    if source_type not in MONTHLY_CLOSE_SOURCE_TYPES:
        raise MonthlyCloseDocumentError("unknown_source_type", "资料类型不存在", 422)
    return source_type


def normalize_current_source_type(source_type: str) -> str:
    """Map a retired input to the one current employee-facing category."""
    validate_source_type(source_type)
    normalized = "utility_expense" if source_type == "utility_receipt" else source_type
    if normalized not in MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES:
        raise MonthlyCloseDocumentError("unknown_source_type", "资料类型不存在", 422)
    return normalized


def validate_document(data: bytes, filename: str) -> str:
    if len(data) > MAX_MONTHLY_CLOSE_DOCUMENT_BYTES:
        raise MonthlyCloseDocumentError("document_too_large", "文件超过 10MB", 413)
    if not data:
        raise MonthlyCloseDocumentError("invalid_document", "文件内容为空", 422)
    try:
        validate_bill_container(data, filename)
    except BillParseError:
        raise MonthlyCloseDocumentError(
            "invalid_document", "请上传有效的 xls/xlsx 文件", 422
        ) from None
    return upload_fingerprint(data)


async def _requirement(
    db: AsyncSession, cycle_id: str, source_type: str, *, for_update: bool = False
) -> MonthlyCloseSourceRequirement:
    statement = select(MonthlyCloseSourceRequirement).where(
        MonthlyCloseSourceRequirement.cycle_id == cycle_id,
        MonthlyCloseSourceRequirement.source_type == source_type,
    )
    if for_update:
        statement = statement.with_for_update()
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        raise MonthlyCloseDocumentError("source_not_initialized", "该资料项尚未初始化")
    return row


async def store_document(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    *,
    source_type: str,
    filename: str,
    mime_type: str,
    data: bytes,
    user_id: str,
    replaces_document_id: str | None = None,
    commit: bool = True,
) -> MonthlyCloseDocument:
    validate_source_type(source_type)
    if replaces_document_id and source_type != "operating_expenses":
        raise MonthlyCloseDocumentError(
            "document_replacement_not_supported",
            "仅运营支出原件支持显式替代关系。",
            422,
        )
    fingerprint = validate_document(data, filename)
    requirement = await _requirement(
        db, cycle.cycle_id, source_type, for_update=True
    )
    existing = (
        await db.execute(
            select(MonthlyCloseDocument)
            .options(undefer(MonthlyCloseDocument.content))
            .where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.sha256 == fingerprint,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if existing is not None:
        if replaces_document_id and replaces_document_id not in set(
            (existing.metadata_ or {}).get("supersedes_document_ids", [])
        ):
            raise MonthlyCloseDocumentError(
                "replacement_requires_new_document",
                "相同原件不能改绑到新的替代关系。",
                409,
            )
        reclassified_from: str | None = None
        if existing.source_type != source_type:
            if existing.is_active:
                raise MonthlyCloseDocumentError(
                    "duplicate_document_other_source", "相同文件已归入其他资料类型"
                )
            reclassified_from = existing.source_type
            await db.execute(
                delete(MonthlyCloseServiceLine).where(
                    MonthlyCloseServiceLine.document_id == existing.document_id
                )
            )
            history = (existing.metadata_ or {}).get(
                "reclassification_history", []
            )
            history = history if isinstance(history, list) else []
            existing.source_type = source_type
            existing.processing_status = "stored"
            existing.processing_error = None
            existing.engine_type = None
            existing.engine_id = None
            existing.metadata_ = {
                "reclassification_history": [
                    *history,
                    {
                        "from": reclassified_from,
                        "to": source_type,
                        "by": user_id,
                        "at": datetime.now(timezone.utc).isoformat(),
                    },
                ]
            }
        if not existing.is_active:
            if source_type == "operating_expenses":
                await _ensure_operating_expense_reactivation_is_unique(db, existing)
            existing.is_active = True
            existing.invalidated_by = None
            existing.invalidated_at = None
            if source_type == "operating_expenses":
                await _set_derived_expenses_active(
                    db,
                    existing,
                    active=True,
                    user_id=user_id,
                    changed_at=datetime.now(timezone.utc),
                )
        requirement.state = "uploaded"
        requirement.not_applicable_reason = None
        requirement.decided_by = user_id
        requirement.decided_at = datetime.now(timezone.utc)
        await db.flush()
        if source_type in {"cleaning_statement", "linen_statement"}:
            if reclassified_from:
                from app.services.monthly_close.layout_memory import find_remembered_mapping
                from app.services.monthly_close.service_reconciliation import process_service_document
                from app.services.monthly_close.service_statement import ServiceStatementMapping

                remembered = await find_remembered_mapping(
                    db,
                    source_type=source_type,
                    data=data,
                    filename=existing.filename,
                )
                mapping = (
                    ServiceStatementMapping(
                        sheet=remembered["sheet"],
                        header_row=remembered["header_row"],
                        columns=remembered["columns"],
                    )
                    if remembered
                    else None
                )
                await process_service_document(
                    db,
                    existing,
                    mapping=mapping,
                    billing_month=cycle.billing_month,
                )
            else:
                await reprocess_active_service_documents(
                    db, cycle.cycle_id, billing_month=cycle.billing_month
                )
        await log_action_tx(
            db,
            user_id,
            (
                "monthly_close.document.reclassify"
                if reclassified_from
                else "monthly_close.document.reactivate"
            ),
            "monthly_close_document",
            existing.document_id,
            before_data={"source_type": reclassified_from} if reclassified_from else None,
            after_data={
                "cycle_id": cycle.cycle_id,
                "billing_month": cycle.billing_month,
                "source_type": source_type,
                "sha256": fingerprint,
            },
        )
        if commit:
            await db.commit()
            await db.refresh(existing)
        else:
            await db.flush()
        return existing

    replacement: MonthlyCloseDocument | None = None
    if replaces_document_id:
        replacement = await db.scalar(
            select(MonthlyCloseDocument)
            .where(
                MonthlyCloseDocument.document_id == replaces_document_id,
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.source_type == "operating_expenses",
            )
            .with_for_update()
        )
        if replacement is None:
            raise MonthlyCloseDocumentError(
                "replacement_document_not_found",
                "指定的运营支出原件不属于当前月结周期。",
                422,
            )

    document = MonthlyCloseDocument(
        document_id="MCD-" + uuid4().hex[:12].upper(),
        cycle_id=cycle.cycle_id,
        source_type=source_type,
        filename=(filename or "document.xlsx")[:255],
        mime_type=(mime_type or "application/octet-stream")[:120],
        byte_size=len(data),
        sha256=fingerprint,
        content=data,
        uploaded_by=user_id,
    )
    if source_type == "operating_expenses":
        lineage = (
            str(
                (replacement.metadata_ or {}).get("operating_event_lineage")
                or replacement.document_id
            )
            if replacement is not None
            else document.document_id
        )
        document.metadata_ = {
            "operating_event_lineage": lineage,
            **(
                {"supersedes_document_ids": [replacement.document_id]}
                if replacement is not None
                else {}
            ),
        }
        if replacement is not None:
            replacement_metadata = dict(replacement.metadata_ or {})
            replacement_metadata.setdefault(
                "operating_event_lineage", replacement.document_id
            )
            replacement_metadata["superseded_by_document_ids"] = sorted(
                {
                    *replacement_metadata.get("superseded_by_document_ids", []),
                    document.document_id,
                }
            )
            replacement.metadata_ = replacement_metadata
    db.add(document)
    await db.flush()
    if source_type in {"cleaning_statement", "linen_statement"}:
        from app.services.monthly_close.layout_memory import find_remembered_mapping
        from app.services.monthly_close.service_reconciliation import (
            process_service_document,
        )
        from app.services.monthly_close.service_statement import ServiceStatementMapping

        remembered = await find_remembered_mapping(
            db,
            source_type=source_type,
            data=data,
            filename=document.filename,
        )
        mapping = (
            ServiceStatementMapping(
                sheet=remembered["sheet"],
                header_row=remembered["header_row"],
                columns=remembered["columns"],
            )
            if remembered
            else None
        )
        await process_service_document(
            db,
            document,
            mapping=mapping,
            billing_month=cycle.billing_month,
        )
    requirement.state = "uploaded"
    requirement.not_applicable_reason = None
    requirement.decided_by = user_id
    requirement.decided_at = datetime.now(timezone.utc)
    await log_action_tx(
        db,
        user_id,
        "monthly_close.document.upload",
        "monthly_close_document",
        document.document_id,
        after_data={
            "cycle_id": cycle.cycle_id,
            "billing_month": cycle.billing_month,
            "source_type": source_type,
            "sha256": fingerprint,
            "byte_size": len(data),
        },
    )
    if commit:
        await db.commit()
        await db.refresh(document)
    else:
        await db.flush()
    return document


async def mark_source_not_applicable(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    *,
    source_type: str,
    reason: str,
    user_id: str,
) -> MonthlyCloseSourceRequirement:
    source_type = normalize_current_source_type(source_type)
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise MonthlyCloseDocumentError(
            "not_applicable_reason_required", "必须填写不适用原因", 422
        )
    requirement = await _requirement(
        db, cycle.cycle_id, source_type, for_update=True
    )
    active_count = await db.scalar(
        select(func.count())
        .select_from(MonthlyCloseDocument)
        .where(
            MonthlyCloseDocument.cycle_id == cycle.cycle_id,
            MonthlyCloseDocument.source_type.in_(
                ("utility_receipt", "utility_expense")
                if source_type == "utility_expense"
                else (source_type,)
            ),
            MonthlyCloseDocument.is_active.is_(True),
        )
    )
    if active_count:
        raise MonthlyCloseDocumentError(
            "source_has_active_documents", "请先归档该资料项下的有效文件"
        )
    now = datetime.now(timezone.utc)
    requirement.state = "not_applicable"
    requirement.not_applicable_reason = normalized_reason
    requirement.decided_by = user_id
    requirement.decided_at = now
    await log_action_tx(
        db,
        user_id,
        "monthly_close.source.not_applicable",
        "monthly_close",
        cycle.cycle_id,
        after_data={
            "billing_month": cycle.billing_month,
            "source_type": source_type,
            "reason": normalized_reason,
        },
    )
    await db.commit()
    await db.refresh(requirement)
    return requirement


async def archive_document(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    *,
    document_id: str,
    user_id: str,
) -> MonthlyCloseDocument:
    document = (
        await db.execute(
            select(MonthlyCloseDocument)
            .where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.document_id == document_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if document is None:
        raise MonthlyCloseDocumentError("document_not_found", "文件不存在", 404)
    if not document.is_active:
        return document
    utility_documents: list[MonthlyCloseDocument] = []
    if document.source_type in {"utility_receipt", "utility_expense"}:
        utility_documents = list(
            await db.scalars(
                select(MonthlyCloseDocument).where(
                    MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                    MonthlyCloseDocument.source_type.in_(
                        ("utility_receipt", "utility_expense")
                    ),
                    MonthlyCloseDocument.is_active.is_(True),
                )
            )
        )
    legacy_utility_contract = uses_legacy_utility_contract(
        cycle, documents=utility_documents
    )
    requirement_source_type = (
        document.source_type
        if legacy_utility_contract
        else normalize_current_source_type(document.source_type)
    )
    requirement = await _requirement(
        db, cycle.cycle_id, requirement_source_type, for_update=True
    )
    now = datetime.now(timezone.utc)
    document.is_active = False
    document.invalidated_by = user_id
    document.invalidated_at = now
    linked_receipt = (
        await db.execute(
            select(MonthlyCloseInboxItem)
            .where(MonthlyCloseInboxItem.document_id == document.document_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if linked_receipt is not None:
        linked_receipt.status = "dismissed"
        linked_receipt.updated_by = user_id
        linked_receipt.classification_reason = (
            "原归档已撤销；如需重新使用，可修改资料类型后再次归档。"
        )
    derived_expense_ids: list[str] = []
    if document.source_type == "operating_expenses":
        newer_active_documents = list(
            (
                await db.execute(
                    select(MonthlyCloseDocument).where(
                        MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                        MonthlyCloseDocument.source_type == "operating_expenses",
                        MonthlyCloseDocument.is_active.is_(True),
                        MonthlyCloseDocument.document_id != document.document_id,
                        MonthlyCloseDocument.uploaded_at >= document.uploaded_at,
                    )
                )
            ).scalars()
        )
        if newer_active_documents:
            document.metadata_ = {
                **(document.metadata_ or {}),
                "superseded_by_document_ids": sorted(
                    item.document_id for item in newer_active_documents
                ),
            }
        derived_expense_ids = await _set_derived_expenses_active(
            db,
            document,
            active=False,
            user_id=user_id,
            changed_at=now,
        )
    await db.flush()
    remaining_source_types = (
        ("utility_receipt", "utility_expense")
        if requirement_source_type == "utility_expense"
        and not legacy_utility_contract
        else (document.source_type,)
    )
    remaining = await db.scalar(
        select(func.count())
        .select_from(MonthlyCloseDocument)
        .where(
            MonthlyCloseDocument.cycle_id == cycle.cycle_id,
            MonthlyCloseDocument.source_type.in_(remaining_source_types),
            MonthlyCloseDocument.is_active.is_(True),
            MonthlyCloseDocument.document_id != document.document_id,
        )
    )
    if not remaining:
        requirement.state = "pending"
        requirement.not_applicable_reason = None
        requirement.decided_by = user_id
        requirement.decided_at = now
    if document.source_type in {"cleaning_statement", "linen_statement"}:
        await reprocess_active_service_documents(
            db, cycle.cycle_id, billing_month=cycle.billing_month
        )
    await log_action_tx(
        db,
        user_id,
        "monthly_close.document.archive",
        "monthly_close_document",
        document.document_id,
        before_data={"is_active": True},
        after_data={
            "is_active": False,
            "cycle_id": cycle.cycle_id,
            "source_type": document.source_type,
            "voided_expense_ids": derived_expense_ids,
        },
    )
    await db.commit()
    await db.refresh(document)
    return document


async def get_document(
    db: AsyncSession, document_id: str
) -> MonthlyCloseDocument | None:
    return (
        await db.execute(
            select(MonthlyCloseDocument)
            .options(undefer(MonthlyCloseDocument.content))
            .where(MonthlyCloseDocument.document_id == document_id)
        )
    ).scalar_one_or_none()


async def reprocess_active_service_documents(
    db: AsyncSession, cycle_id: str, *, billing_month: str | None = None
) -> None:
    """Recalculate duplicate status after a service source is added or removed."""
    from app.services.monthly_close.service_reconciliation import (
        process_service_document,
    )
    from app.services.monthly_close.service_statement import ServiceStatementMapping

    documents = list(
        (
            await db.execute(
                select(MonthlyCloseDocument)
                .options(undefer(MonthlyCloseDocument.content))
                .where(
                    MonthlyCloseDocument.cycle_id == cycle_id,
                    MonthlyCloseDocument.source_type.in_(
                        ("cleaning_statement", "linen_statement")
                    ),
                    MonthlyCloseDocument.is_active.is_(True),
                )
            )
        ).scalars()
    )
    for active in documents:
        stored = (active.metadata_ or {}).get("mapping")
        mapping = (
            ServiceStatementMapping(
                sheet=stored["sheet"],
                header_row=stored["header_row"],
                columns=stored["columns"],
            )
            if isinstance(stored, dict)
            and {"sheet", "header_row", "columns"} <= set(stored)
            else None
        )
        await process_service_document(
            db, active, mapping=mapping, billing_month=billing_month
        )
