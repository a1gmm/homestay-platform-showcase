"""Durable upload-only intake before a workbook joins monthly-close evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from hashlib import sha256
import secrets
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models.monthly_close import (
    MonthlyCloseCycle,
    MonthlyCloseInboxItem,
    MonthlyCloseIntakeLink,
)
from app.services.audit import log_action_tx
from app.services.monthly_close.documents import (
    MonthlyCloseDocumentError,
    store_document,
    validate_document,
    validate_source_type,
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
) -> MonthlyCloseInboxItem:
    if origin not in {"admin", "external"}:
        raise MonthlyCloseInboxError("invalid_inbox_origin", "收件来源无效", 422)
    if source_type is not None:
        validate_source_type(source_type)
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
    await db.commit()
    await db.refresh(item)
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
    item.source_type = result.source_type
    item.updated_by = user_id
    item.confidence = Decimal(str(result.confidence)).quantize(Decimal("0.0001"))
    item.suggested_by = result.suggested_by
    item.classification_reason = result.reason
    item.status = "classified" if result.confidence >= 0.8 else "needs_review"
    item.last_error = None
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
    validate_source_type(source_type)
    item = await _inbox_item(db, cycle.cycle_id, item_id, for_update=True)
    if item.status == "confirmed":
        raise MonthlyCloseInboxError("inbox_item_confirmed", "已归档文件不能修改分类")
    previous = item.source_type
    item.source_type = source_type
    item.updated_by = user_id
    item.confidence = Decimal("1.0000")
    item.suggested_by = "administrator"
    item.classification_reason = "已由管理员确认资料类型"
    item.status = "classified"
    item.last_error = None
    await log_action_tx(
        db,
        user_id,
        "monthly_close.inbox.source.correct",
        "monthly_close_inbox",
        item.item_id,
        before_data={"source_type": previous},
        after_data={"source_type": source_type},
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
    try:
        document = await store_document(
            db,
            cycle,
            source_type=item.source_type,
            filename=item.filename,
            mime_type=item.mime_type,
            data=item.content,
            user_id=user_id,
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
    await log_action_tx(
        db,
        user_id,
        "monthly_close.inbox.confirm",
        "monthly_close_inbox",
        item.item_id,
        after_data={
            "document_id": document.document_id,
            "source_type": document.source_type,
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
    source_type: str | None,
    user_id: str,
    valid_days: int = 45,
) -> tuple[MonthlyCloseIntakeLink, str]:
    normalized_label = label.strip()
    if not normalized_label:
        raise MonthlyCloseInboxError("intake_label_required", "请填写收件链接名称", 422)
    if source_type is not None:
        validate_source_type(source_type)
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
    db: AsyncSession, token: str
) -> tuple[MonthlyCloseIntakeLink, MonthlyCloseCycle]:
    token_hash = sha256(token.encode()).hexdigest()
    row = (
        await db.execute(
            select(MonthlyCloseIntakeLink, MonthlyCloseCycle)
            .join(
                MonthlyCloseCycle,
                MonthlyCloseCycle.cycle_id == MonthlyCloseIntakeLink.cycle_id,
            )
            .where(MonthlyCloseIntakeLink.token_hash == token_hash)
        )
    ).one_or_none()
    if row is None:
        raise MonthlyCloseInboxError("intake_link_invalid", "收件链接无效", 404)
    link, cycle = row
    now = datetime.now(timezone.utc)
    if link.revoked_at is not None or _utc(link.expires_at) <= now:
        raise MonthlyCloseInboxError("intake_link_expired", "收件链接已失效", 410)
    if cycle.status == "completed":
        raise MonthlyCloseInboxError("intake_cycle_completed", "该月月结已完成，暂不再收件", 410)
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
