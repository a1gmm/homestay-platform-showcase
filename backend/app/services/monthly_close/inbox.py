"""Durable upload-only intake before a workbook joins monthly-close evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import secrets
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models.monthly_close import (
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseInboxItem,
    MonthlyCloseIntakeLink,
)
from app.models.monthly_close_control import (
    MonthlyCloseDocumentAnalysis,
    MonthlyCloseOtaSettlementConsumption,
    MonthlyCloseOutbox,
    MonthlyCloseProcessingJob,
    MonthlyCloseProposal,
)
from app.services.audit import log_action_tx
from app.services.monthly_close.documents import (
    MonthlyCloseDocumentError,
    normalize_current_source_type,
    store_document,
    validate_document,
)
from app.services.monthly_close.source_classifier import classify_monthly_close_source


class MonthlyCloseInboxError(RuntimeError):
    def __init__(self, code: str, message: str, status_code: int = 409) -> None:
        self.code = code
        self.message = message
        self.status_code = status_code
        super().__init__(message)

    def to_detail(self) -> dict[str, str]:
        return {"code": self.code, "message": self.message}


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def inbox_item_view(item: MonthlyCloseInboxItem) -> dict[str, Any]:
    return {
        "item_id": item.item_id,
        "filename": item.filename,
        "byte_size": item.byte_size,
        "source_type": item.source_type,
        "confidence": float(item.confidence) if item.confidence is not None else None,
        "suggested_by": item.suggested_by,
        "classification_reason": item.classification_reason,
        "classification_generation": item.classification_generation,
        "status": item.status,
        "last_error": item.last_error,
        "origin": item.origin,
        "submitted_label": item.submitted_label,
        "document_id": item.document_id,
        "created_at": item.created_at.isoformat() if item.created_at else None,
        "updated_at": item.updated_at.isoformat() if item.updated_at else None,
    }


def intake_link_view(link: MonthlyCloseIntakeLink) -> dict[str, Any]:
    return {
        "link_id": link.link_id,
        "source_type": link.source_type,
        "label": link.label,
        "expires_at": link.expires_at.isoformat(),
        "revoked_at": link.revoked_at.isoformat() if link.revoked_at else None,
        "created_at": link.created_at.isoformat() if link.created_at else None,
        "last_uploaded_at": (
            link.last_uploaded_at.isoformat() if link.last_uploaded_at else None
        ),
    }


async def list_inbox_items(
    db: AsyncSession, cycle_id: str
) -> list[MonthlyCloseInboxItem]:
    return list(
        (
            await db.execute(
                select(MonthlyCloseInboxItem)
                .where(MonthlyCloseInboxItem.cycle_id == cycle_id)
                .order_by(
                    MonthlyCloseInboxItem.created_at.desc(),
                    MonthlyCloseInboxItem.item_id.desc(),
                )
            )
        ).scalars()
    )


async def apply_inbox_source_hint(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    item_id: str,
    *,
    source_type: str,
    actor_id: str,
    actor_role: str,
) -> MonthlyCloseInboxItem | None:
    """Apply a reversible source hint from the uploader's plain-language note.

    This changes intake metadata only.  The file still needs an explicit
    classification confirmation before it becomes canonical monthly-close
    evidence or starts analysis.
    """
    normalized_source = normalize_current_source_type(source_type)
    low_role_sources = {
        "cleaner": {"cleaning_statement"},
        "keeper": {"cleaning_statement", "linen_statement"},
    }
    if actor_role in low_role_sources and normalized_source not in low_role_sources[actor_role]:
        return None
    if actor_role not in {"admin", "finance", "operator", "cleaner", "keeper"}:
        return None

    # Match the processing worker's cycle -> item lock order and keep closed
    # month intake immutable while still allowing the surrounding read query.
    locked_cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_cycle is None or locked_cycle.status == "completed":
        return None
    item = await _inbox_item(db, cycle.cycle_id, item_id, for_update=True)
    if actor_role in low_role_sources and item.created_by != actor_id:
        return None
    if item.status in {"confirmed", "dismissed"} or item.document_id is not None:
        return None

    previous = item.source_type
    if previous is not None and previous != normalized_source:
        item.classification_generation += 1
    item.source_type = normalized_source
    item.updated_by = actor_id
    item.confidence = Decimal("1.0000")
    item.suggested_by = "user_description"
    item.classification_reason = "由提交人的说明判断资料类型，仍需确认"
    item.status = "needs_review"
    item.last_error = None

    from app.services.monthly_close.processing import complete_classification_manually

    await complete_classification_manually(
        db,
        item,
        actor_id=actor_id,
        classification_source="user_description",
    )
    await log_action_tx(
        db,
        actor_id,
        "monthly_close.inbox.source.hint",
        "monthly_close_inbox",
        item.item_id,
        before_data={"source_type": previous},
        after_data={
            "source_type": normalized_source,
            "classification_generation": item.classification_generation,
            "requires_confirmation": True,
        },
        notes="根据提交人说明生成可撤销的资料分类提示",
    )
    await db.flush()
    return item


def _contains_identifier(value: Any, identifier: str) -> bool:
    """Find an exact document identifier in a persisted proposal payload."""
    if isinstance(value, dict):
        return identifier in value or any(
            _contains_identifier(item, identifier) for item in value.values()
        )
    if isinstance(value, (list, tuple)):
        return any(_contains_identifier(item, identifier) for item in value)
    return value == identifier


async def permanently_delete_inbox_item(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    item_id: str,
    *,
    user_id: str,
) -> None:
    """Hard-delete a dismissed source blob while preserving a metadata-only audit tombstone."""
    item = await _inbox_item(db, cycle.cycle_id, item_id, for_update=True)
    if item.status != "dismissed":
        raise MonthlyCloseInboxError(
            "inbox_item_not_dismissed",
            "请先撤销这份资料，再永久删除",
        )

    document: MonthlyCloseDocument | None = None
    if item.document_id:
        document = (
            await db.execute(
                select(MonthlyCloseDocument)
                .where(
                    MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                    MonthlyCloseDocument.document_id == item.document_id,
                )
                .with_for_update()
            )
        ).scalar_one_or_none()
        if document is not None and document.is_active:
            raise MonthlyCloseInboxError(
                "inbox_document_still_active",
                "这份资料仍在本月账目中使用，请先撤销后再删除",
            )

    document_id = document.document_id if document is not None else None
    analysis_job_ids: set[str] = set()
    if document_id:
        from app.models.cleaning_work_record import CleaningWorkImport
        work_import = await db.scalar(select(CleaningWorkImport.import_id).where(CleaningWorkImport.document_id == document_id).limit(1))
        if work_import is not None:
            raise MonthlyCloseInboxError("inbox_item_work_record_referenced", "这份原表已用于补齐历史打扫记录，需要保留作为核对依据，不能永久删除")

        proposals = list(
            await db.scalars(
                select(MonthlyCloseProposal).where(
                    MonthlyCloseProposal.cycle_id == cycle.cycle_id
                )
            )
        )
        if any(
            _contains_identifier(
                {
                    "evidence_refs": proposal.evidence_refs,
                    "canonical_payload": proposal.canonical_payload,
                    "mapping_versions": proposal.mapping_versions,
                    "subject_versions": proposal.subject_versions,
                },
                document_id,
            )
            for proposal in proposals
        ):
            raise MonthlyCloseInboxError(
                "inbox_item_financially_referenced",
                "这份资料已进入正式对账或审批记录，为保护账目证据不能删除",
            )
        settlement_reference = await db.scalar(
            select(MonthlyCloseOtaSettlementConsumption.consumption_id).where(
                MonthlyCloseOtaSettlementConsumption.later_document_id == document_id
            )
        )
        if settlement_reference is not None:
            raise MonthlyCloseInboxError(
                "inbox_item_financially_referenced",
                "这份资料已进入正式结算记录，为保护账目证据不能删除",
            )
        analysis_job_ids = set(
            await db.scalars(
                select(MonthlyCloseDocumentAnalysis.job_id).where(
                    MonthlyCloseDocumentAnalysis.document_id == document_id
                )
            )
        )

    classification_jobs = list(
        await db.scalars(
            select(MonthlyCloseProcessingJob).where(
                MonthlyCloseProcessingJob.cycle_id == cycle.cycle_id,
                MonthlyCloseProcessingJob.job_type == "classify_document",
                MonthlyCloseProcessingJob.subject_id == item.item_id,
            ).with_for_update()
        )
    )
    related_job_ids = analysis_job_ids | {job.job_id for job in classification_jobs}
    related_jobs = (
        list(
            await db.scalars(
                select(MonthlyCloseProcessingJob).where(
                    MonthlyCloseProcessingJob.job_id.in_(related_job_ids)
                ).with_for_update()
            )
        )
        if related_job_ids
        else []
    )
    if any(job.status == "leased" for job in related_jobs):
        raise MonthlyCloseInboxError(
            "inbox_item_processing_active",
            "文件仍在后台处理中，请稍后再永久删除",
        )

    await log_action_tx(
        db,
        user_id,
        "monthly_close.inbox.permanent_delete",
        "monthly_close_inbox",
        item.item_id,
        after_data={
            "billing_month": cycle.billing_month,
            "cycle_id": cycle.cycle_id,
            "document_id": document_id,
            "source_type": item.source_type,
            "sha256": item.sha256,
            "byte_size": item.byte_size,
        },
        notes="管理员确认永久删除月结原文件；文件名和内容未保留",
    )

    if related_job_ids:
        outboxes = list(
            await db.scalars(
                select(MonthlyCloseOutbox).where(
                    MonthlyCloseOutbox.cycle_id == cycle.cycle_id
                )
            )
        )
        obsolete_outbox_ids = [
            outbox.outbox_id
            for outbox in outboxes
            if (outbox.payload or {}).get("job_id") in related_job_ids
        ]
        if obsolete_outbox_ids:
            await db.execute(
                delete(MonthlyCloseOutbox).where(
                    MonthlyCloseOutbox.outbox_id.in_(obsolete_outbox_ids)
                )
            )
    if document_id:
        await db.execute(
            delete(MonthlyCloseDocumentAnalysis).where(
                MonthlyCloseDocumentAnalysis.document_id == document_id
            )
        )
    if related_job_ids:
        await db.execute(
            delete(MonthlyCloseProcessingJob).where(
                MonthlyCloseProcessingJob.job_id.in_(related_job_ids)
            )
        )
    await db.execute(
        delete(MonthlyCloseInboxItem).where(
            MonthlyCloseInboxItem.item_id == item.item_id
        )
    )
    if document_id:
        await db.execute(
            delete(MonthlyCloseDocument).where(
                MonthlyCloseDocument.document_id == document_id
            )
        )
    await db.commit()


async def _inbox_item(
    db: AsyncSession,
    cycle_id: str,
    item_id: str,
    *,
    with_content: bool = False,
    for_update: bool = False,
) -> MonthlyCloseInboxItem:
    statement = select(MonthlyCloseInboxItem).where(
        MonthlyCloseInboxItem.cycle_id == cycle_id,
        MonthlyCloseInboxItem.item_id == item_id,
    )
    if with_content:
        statement = statement.options(undefer(MonthlyCloseInboxItem.content))
    if for_update:
        statement = statement.with_for_update()
    item = (await db.execute(statement)).scalar_one_or_none()
    if item is None:
        raise MonthlyCloseInboxError("inbox_item_not_found", "收件记录不存在", 404)
    return item


async def receive_inbox_item(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    *,
    filename: str,
    mime_type: str,
    data: bytes,
    origin: str,
    user_id: str | None,
    source_type: str | None = None,
    submitted_label: str | None = None,
    commit: bool = True,
) -> MonthlyCloseInboxItem:
    if origin not in {"admin", "external"}:
        raise MonthlyCloseInboxError("invalid_inbox_origin", "收件来源无效", 422)
    if source_type is not None:
        source_type = normalize_current_source_type(source_type)
    fingerprint = validate_document(data, filename)
    existing = (
        await db.execute(
            select(MonthlyCloseInboxItem).where(
                MonthlyCloseInboxItem.cycle_id == cycle.cycle_id,
                MonthlyCloseInboxItem.sha256 == fingerprint,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    preset = source_type is not None
    item = MonthlyCloseInboxItem(
        item_id="MCI-" + uuid4().hex[:12].upper(),
        cycle_id=cycle.cycle_id,
        filename=(filename or "document.xlsx")[:255],
        mime_type=(mime_type or "application/octet-stream")[:120],
        byte_size=len(data),
        sha256=fingerprint,
        content=data,
        source_type=source_type,
        confidence=Decimal("1.0000") if preset else None,
        suggested_by="external_link" if preset else None,
        classification_reason=(
            f"由“{submitted_label}”上传链接指定资料类型" if preset else None
        ),
        status="classified" if preset else "received",
        origin=origin,
        submitted_label=(submitted_label or "")[:120] or None,
        created_by=user_id,
        updated_by=user_id,
    )
    db.add(item)
    await log_action_tx(
        db,
        user_id,
        "monthly_close.inbox.receive",
        "monthly_close_inbox",
        item.item_id,
        after_data={
            "cycle_id": cycle.cycle_id,
            "billing_month": cycle.billing_month,
            "origin": origin,
            "source_type": source_type,
            "sha256": fingerprint,
            "byte_size": len(data),
        },
    )
    if commit:
        await db.commit()
        await db.refresh(item)
    else:
        await db.flush()
    return item


async def classify_inbox_item(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    item_id: str,
    *,
    user_id: str,
) -> MonthlyCloseInboxItem:
    item = await _inbox_item(
        db, cycle.cycle_id, item_id, with_content=True, for_update=True
    )
    if item.status == "confirmed":
        return item
    try:
        result = await classify_monthly_close_source(item.content, item.filename)
    except Exception as exc:
        item.status = "failed"
        item.last_error = "自动识别失败，请重试或手动选择资料类型"
        await db.commit()
        raise MonthlyCloseInboxError(
            "inbox_classification_failed", item.last_error, 503
        ) from exc
    if (
        item.document_id is not None
        and item.source_type is not None
        and item.source_type != result.source_type
    ):
        item.classification_generation += 1
    item.source_type = result.source_type
    item.updated_by = user_id
    item.confidence = Decimal(str(result.confidence)).quantize(Decimal("0.0001"))
    item.suggested_by = result.suggested_by
    item.classification_reason = result.reason
    item.status = "classified" if result.confidence >= 0.8 else "needs_review"
    item.last_error = None
    from app.services.monthly_close.processing import complete_classification_manually

    await complete_classification_manually(
        db,
        item,
        actor_id=user_id,
        classification_source="automatic_classifier",
    )
    await log_action_tx(
        db,
        user_id,
        "monthly_close.inbox.classify",
        "monthly_close_inbox",
        item.item_id,
        after_data={
            "source_type": item.source_type,
            "confidence": str(item.confidence),
            "suggested_by": item.suggested_by,
        },
    )
    await db.commit()
    await db.refresh(item)
    return item


async def set_inbox_source(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    item_id: str,
    *,
    source_type: str,
    user_id: str,
) -> MonthlyCloseInboxItem:
    source_type = normalize_current_source_type(source_type)
    item = await _inbox_item(db, cycle.cycle_id, item_id, for_update=True)
    if item.status == "confirmed":
        raise MonthlyCloseInboxError("inbox_item_confirmed", "已归档文件不能修改分类")
    previous = item.source_type
    if (
        item.document_id is not None
        and previous is not None
        and previous != source_type
    ):
        item.classification_generation += 1
    item.source_type = source_type
    item.updated_by = user_id
    item.confidence = Decimal("1.0000")
    item.suggested_by = "administrator"
    item.classification_reason = "已由管理员确认资料类型"
    item.status = "classified"
    item.last_error = None
    from app.services.monthly_close.processing import complete_classification_manually

    await complete_classification_manually(db, item, actor_id=user_id)
    await log_action_tx(
        db,
        user_id,
        "monthly_close.inbox.source.correct",
        "monthly_close_inbox",
        item.item_id,
        before_data={"source_type": previous},
        after_data={
            "source_type": source_type,
            "classification_generation": item.classification_generation,
        },
    )
    await db.commit()
    await db.refresh(item)
    return item


async def confirm_inbox_item(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    item_id: str,
    *,
    user_id: str,
) -> MonthlyCloseInboxItem:
    item = await _inbox_item(
        db, cycle.cycle_id, item_id, with_content=True, for_update=True
    )
    if item.status == "confirmed" and item.document_id:
        return item
    if not item.source_type:
        raise MonthlyCloseInboxError(
            "inbox_source_required", "请先确认该文件属于哪一类月结资料", 422
        )
    normalized_source_type = normalize_current_source_type(item.source_type)
    if normalized_source_type != item.source_type:
        item.source_type = normalized_source_type
        item.classification_generation += 1
        item.confidence = Decimal("1.0000")
        item.suggested_by = "system"
        item.classification_reason = "旧版水电分类已统一为水电支出"
    try:
        document = await store_document(
            db,
            cycle,
            source_type=item.source_type,
            filename=item.filename,
            mime_type=item.mime_type,
            data=item.content,
            user_id=user_id,
            commit=False,
        )
    except MonthlyCloseDocumentError as exc:
        item.status = "failed"
        item.last_error = exc.message
        await db.commit()
        raise
    item.status = "confirmed"
    item.updated_by = user_id
    item.document_id = document.document_id
    item.last_error = None
    from app.services.monthly_close.processing import enqueue_analysis_job

    await enqueue_analysis_job(db, item, document)
    await log_action_tx(
        db,
        user_id,
        "monthly_close.inbox.confirm",
        "monthly_close_inbox",
        item.item_id,
        after_data={
            "document_id": document.document_id,
            "source_type": document.source_type,
            "classification_generation": item.classification_generation,
        },
    )
    await db.commit()
    await db.refresh(item)
    return item


async def create_intake_link(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    *,
    label: str,
    source_type: str,
    user_id: str,
    valid_days: int = 45,
) -> tuple[MonthlyCloseIntakeLink, str]:
    normalized_label = label.strip()
    if not normalized_label:
        raise MonthlyCloseInboxError("intake_label_required", "请填写收件链接名称", 422)
    source_type = normalize_current_source_type(source_type)
    now = datetime.now(timezone.utc)
    existing = list(
        (
            await db.execute(
                select(MonthlyCloseIntakeLink)
                .where(
                    MonthlyCloseIntakeLink.cycle_id == cycle.cycle_id,
                    MonthlyCloseIntakeLink.source_type == source_type,
                    MonthlyCloseIntakeLink.revoked_at.is_(None),
                )
                .with_for_update()
            )
        ).scalars()
    )
    for existing_link in existing:
        existing_link.revoked_at = now
    token = secrets.token_urlsafe(32)
    link = MonthlyCloseIntakeLink(
        link_id="MCIL-" + uuid4().hex[:12].upper(),
        cycle_id=cycle.cycle_id,
        token_hash=sha256(token.encode()).hexdigest(),
        source_type=source_type,
        label=normalized_label[:120],
        expires_at=now + timedelta(days=valid_days),
        created_by=user_id,
    )
    db.add(link)
    await log_action_tx(
        db,
        user_id,
        "monthly_close.intake_link.create",
        "monthly_close_intake_link",
        link.link_id,
        after_data={
            "cycle_id": cycle.cycle_id,
            "billing_month": cycle.billing_month,
            "source_type": source_type,
            "expires_at": link.expires_at.isoformat(),
        },
    )
    await db.commit()
    await db.refresh(link)
    return link, token


async def list_intake_links(
    db: AsyncSession, cycle_id: str
) -> list[MonthlyCloseIntakeLink]:
    return list(
        (
            await db.execute(
                select(MonthlyCloseIntakeLink)
                .where(MonthlyCloseIntakeLink.cycle_id == cycle_id)
                .order_by(MonthlyCloseIntakeLink.created_at.desc())
            )
        ).scalars()
    )


async def resolve_intake_link(
    db: AsyncSession, token: str, *, for_update: bool = False
) -> tuple[MonthlyCloseIntakeLink, MonthlyCloseCycle]:
    token_hash = sha256(token.encode()).hexdigest()
    statement = (
        select(MonthlyCloseIntakeLink, MonthlyCloseCycle)
            .join(
                MonthlyCloseCycle,
                MonthlyCloseCycle.cycle_id == MonthlyCloseIntakeLink.cycle_id,
            )
            .where(MonthlyCloseIntakeLink.token_hash == token_hash)
    )
    if for_update:
        statement = statement.with_for_update(of=MonthlyCloseIntakeLink).execution_options(
            populate_existing=True
        )
    row = (await db.execute(statement)).one_or_none()
    if row is None:
        raise MonthlyCloseInboxError("intake_link_invalid", "收件链接无效", 404)
    link, cycle = row
    now = datetime.now(timezone.utc)
    if (
        link.source_type is None
        or link.revoked_at is not None
        or _utc(link.expires_at) <= now
        or cycle.status == "completed"
    ):
        # Public capability states are intentionally indistinguishable.  In
        # particular, legacy wildcard links fail closed until replaced.
        raise MonthlyCloseInboxError("intake_link_invalid", "收件链接无效", 404)
    return link, cycle


async def revoke_intake_link(
    db: AsyncSession,
    cycle_id: str,
    link_id: str,
    *,
    user_id: str,
) -> MonthlyCloseIntakeLink:
    link = (
        await db.execute(
            select(MonthlyCloseIntakeLink)
            .where(
                MonthlyCloseIntakeLink.cycle_id == cycle_id,
                MonthlyCloseIntakeLink.link_id == link_id,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if link is None:
        raise MonthlyCloseInboxError("intake_link_not_found", "收件链接不存在", 404)
    if link.revoked_at is None:
        link.revoked_at = datetime.now(timezone.utc)
        await log_action_tx(
            db,
            user_id,
            "monthly_close.intake_link.revoke",
            "monthly_close_intake_link",
            link.link_id,
        )
        await db.commit()
        await db.refresh(link)
    return link
