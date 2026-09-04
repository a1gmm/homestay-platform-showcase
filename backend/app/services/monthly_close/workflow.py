"""Transaction-safe state machine for the administrator monthly close."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
import hashlib
import re
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monthly_close import (
    MONTHLY_CLOSE_SOURCE_TYPES,
    MONTHLY_CLOSE_STEP_KEYS,
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseInboxItem,
    MonthlyCloseSourceRequirement,
    MonthlyCloseStepConfirmation,
)
from app.models.user import User
from app.services.audit import log_action_tx
from app.services.monthly_close.evidence import (
    StepEvidence,
    build_step_evidences,
    step_confirmation,
)


STEP_LABELS = {
    "source_collection": "资料收集",
    "order_integrity": "订单完整性核对",
    "service_fees": "保洁、布草与服务费核对",
    "utilities": "水电及运营支出核对",
    "ota_statements": "OTA平台账单核对",
    "exception_clearance": "统一处理全部异常",
    "preflight": "月结体检",
    "settlement_review": "生成并复核业主结算单",
    "owner_confirmation": "业主确认，完成月结",
}

EvidenceLoader = Callable[
    [AsyncSession, MonthlyCloseCycle], Awaitable[list[StepEvidence]]
]


class MonthlyCloseConflict(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(message)

    def to_detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.detail}


def validate_billing_month(value: str) -> str:
    if not re.fullmatch(r"\d{4}-\d{2}", value or ""):
        raise MonthlyCloseConflict(
            "invalid_billing_month", "月份必须使用 YYYY-MM 格式"
        )
    year, month = (int(part) for part in value.split("-"))
    if year < 2000 or year > 2200 or month < 1 or month > 12:
        raise MonthlyCloseConflict(
            "invalid_billing_month", "月份必须是有效的自然月"
        )
    return value


async def get_cycle_by_month(
    db: AsyncSession, billing_month: str, *, for_update: bool = False
) -> MonthlyCloseCycle | None:
    validate_billing_month(billing_month)
    statement = select(MonthlyCloseCycle).where(
        MonthlyCloseCycle.billing_month == billing_month
    )
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def get_or_create_cycle(
    db: AsyncSession, billing_month: str, user_id: str
) -> MonthlyCloseCycle:
    billing_month = validate_billing_month(billing_month)
    existing = await get_cycle_by_month(db, billing_month)
    if existing is not None:
        return existing

    cycle = MonthlyCloseCycle(
        cycle_id="MCL-" + uuid4().hex[:12].upper(),
        billing_month=billing_month,
        created_by=user_id,
    )
    try:
        async with db.begin_nested():
            db.add(cycle)
            for source_type in MONTHLY_CLOSE_SOURCE_TYPES:
                db.add(
                    MonthlyCloseSourceRequirement(
                        requirement_id="MCR-" + uuid4().hex[:12].upper(),
                        cycle_id=cycle.cycle_id,
                        source_type=source_type,
                    )
                )
            await log_action_tx(
                db,
                user_id,
                "monthly_close.create",
                "monthly_close",
                cycle.cycle_id,
                after_data={"billing_month": billing_month, "step_count": 9},
            )
            await db.flush()
    except IntegrityError:
        # A concurrent request may have inserted the unique month while this
        # transaction was waiting. The savepoint keeps the session usable and
        # lets both callers return the same durable cycle.
        winner = await get_cycle_by_month(db, billing_month)
        if winner is None:
            raise
        await db.commit()
        return winner
    await db.commit()
    await db.refresh(cycle)
    return cycle


async def require_cycle_writable(
    db: AsyncSession, cycle: MonthlyCloseCycle
) -> MonthlyCloseCycle:
    """Lock the cycle and reject writes until an administrator reopens it."""
    locked = (
        await db.execute(
            select(MonthlyCloseCycle)
            .where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
            .with_for_update()
        )
    ).scalar_one()
    if locked.status == "completed":
        raise MonthlyCloseConflict(
            "cycle_completed", "本月月结已完成，如需修改请先填写原因重新打开"
        )
    return locked


def _effective_steps(
    evidences: list[StepEvidence],
    confirmations: dict[str, MonthlyCloseStepConfirmation],
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    previous_confirmed = True
    for position, evidence in enumerate(evidences, 1):
        confirmation = confirmations.get(evidence.step_key)
        matches = (
            confirmation is not None
            and confirmation.evidence_hash == evidence.evidence_hash
        )
        if confirmation is not None and not matches:
            status = "stale"
        elif not previous_confirmed:
            status = "stale" if confirmation is not None else "locked"
        elif evidence.blocking_count:
            status = "stale" if confirmation is not None else "blocked"
        elif matches:
            status = "confirmed"
        else:
            status = "ready"
        current_confirmed = status == "confirmed"
        confirmation_copy = {
            "confirmation_title": evidence.snapshot["confirmation_title"],
            "confirmation_items": evidence.snapshot["confirmation_items"],
        }
        result.append(
            {
                "position": position,
                "step_key": evidence.step_key,
                "label": STEP_LABELS[evidence.step_key],
                "status": status,
                "evidence_hash": evidence.evidence_hash,
                "blocking_count": evidence.blocking_count,
                "summary": evidence.snapshot["summary"],
                "issues": evidence.snapshot["issues"],
                **confirmation_copy,
                "confirmed_by": (
                    confirmation.confirmed_by if confirmation is not None else None
                ),
                "confirmed_at": (
                    confirmation.confirmed_at.isoformat()
                    if confirmation is not None and confirmation.confirmed_at
                    else None
                ),
            }
        )
        previous_confirmed = previous_confirmed and current_confirmed
    return result


async def build_cycle_view(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    *,
    evidence_loader: EvidenceLoader | None = None,
) -> dict[str, Any]:
    # Final-step confirmation commits and expires ORM attributes. Refresh this
    # server timestamp explicitly so response serialization never attempts
    # implicit async IO outside a greenlet.
    await db.refresh(cycle, attribute_names=["updated_at"])
    loader = evidence_loader or build_step_evidences
    evidences = await loader(db, cycle)
    confirmation_rows = list(
        (
            await db.execute(
                select(MonthlyCloseStepConfirmation).where(
                    MonthlyCloseStepConfirmation.cycle_id == cycle.cycle_id
                )
            )
        ).scalars()
    )
    confirmations = {item.step_key: item for item in confirmation_rows}
    steps = _effective_steps(evidences, confirmations)
    first_incomplete = next(
        (item["step_key"] for item in steps if item["status"] != "confirmed"), None
    )
    effective_status = cycle.status
    if cycle.status == "completed" and first_incomplete is not None:
        effective_status = "needs_recheck"

    requirement_rows = list(
        (
            await db.execute(
                select(MonthlyCloseSourceRequirement).where(
                    MonthlyCloseSourceRequirement.cycle_id == cycle.cycle_id
                )
            )
        ).scalars()
    )
    by_source = {item.source_type: item for item in requirement_rows}
    document_rows = list(
        (
            await db.execute(
                select(MonthlyCloseDocument).where(
                    MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                    MonthlyCloseDocument.is_active.is_(True),
                )
            )
        ).scalars()
    )
    documents_by_source: dict[str, list[MonthlyCloseDocument]] = {}
    for document in document_rows:
        documents_by_source.setdefault(document.source_type, []).append(document)
    sources = [
        {
            "source_type": source_type,
            "state": by_source[source_type].state,
            "not_applicable_reason": by_source[source_type].not_applicable_reason,
            "decided_by": by_source[source_type].decided_by,
            "decided_at": (
                by_source[source_type].decided_at.isoformat()
                if by_source[source_type].decided_at
                else None
            ),
            "documents": [
                {
                    "document_id": document.document_id,
                    "filename": document.filename,
                    "byte_size": document.byte_size,
                    "processing_status": document.processing_status,
                    "processing_error": document.processing_error,
                    "engine_type": document.engine_type,
                    "engine_id": document.engine_id,
                    "uploaded_at": (
                        document.uploaded_at.isoformat()
                        if document.uploaded_at
                        else None
                    ),
                }
                for document in sorted(
                    documents_by_source.get(source_type, []),
                    key=lambda item: (item.uploaded_at, item.document_id),
                )
            ],
        }
        for source_type in MONTHLY_CLOSE_SOURCE_TYPES
        if source_type in by_source
    ]
    return {
        "cycle_id": cycle.cycle_id,
        "billing_month": cycle.billing_month,
        "status": effective_status,
        "stored_status": cycle.status,
        "current_step": first_incomplete,
        "progress": sum(item["status"] == "confirmed" for item in steps),
        "steps": steps,
        "sources": sources,
        "completed_by": cycle.completed_by,
        "completed_at": cycle.completed_at.isoformat() if cycle.completed_at else None,
        "reopen_reason": cycle.reopen_reason,
        "updated_at": cycle.updated_at.isoformat() if cycle.updated_at else None,
    }


async def build_cycle_summary(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    *,
    evidence_loader: EvidenceLoader | None = None,
) -> dict[str, Any]:
    """Return the control-tower contract without full evidence or document lists."""
    view = await build_cycle_view(db, cycle, evidence_loader=evidence_loader)
    current = next(
        (step for step in view["steps"] if step["step_key"] == view["current_step"]),
        None,
    )
    missing_sources = [
        source["source_type"]
        for source in view["sources"]
        if source["state"] == "pending"
    ]
    inbox_rows = list(
        (
            await db.execute(
                select(MonthlyCloseInboxItem).where(
                    MonthlyCloseInboxItem.cycle_id == cycle.cycle_id
                )
            )
        ).scalars()
    )
    inbox_pending_count = sum(
        item.status not in {"confirmed", "dismissed"} for item in inbox_rows
    )
    document_rows = list(
        (
            await db.execute(
                select(MonthlyCloseDocument).where(
                    MonthlyCloseDocument.cycle_id == cycle.cycle_id
                )
            )
        ).scalars()
    )
    activities: list[tuple[datetime, str | None, str | None]] = []

    def add_activity(
        at: datetime | None,
        actor_id: str | None,
        actor_label: str | None = None,
    ) -> None:
        if at is None:
            return
        normalized = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
        activities.append((normalized, actor_id, actor_label))

    add_activity(cycle.created_at, cycle.created_by)
    add_activity(cycle.completed_at, cycle.completed_by)
    add_activity(cycle.reopened_at, cycle.reopened_by)
    for source in view["sources"]:
        add_activity(
            datetime.fromisoformat(source["decided_at"])
            if source["decided_at"]
            else None,
            source["decided_by"],
        )
    for step in view["steps"]:
        add_activity(
            datetime.fromisoformat(step["confirmed_at"])
            if step["confirmed_at"]
            else None,
            step["confirmed_by"],
        )
    for document in document_rows:
        add_activity(document.uploaded_at, document.uploaded_by)
        add_activity(document.invalidated_at, document.invalidated_by)
    for item in inbox_rows:
        add_activity(
            item.updated_at,
            item.updated_by or item.created_by,
            item.submitted_label or ("外部上传" if item.origin == "external" else None),
        )
    latest_at, actor_id, actor_label = max(
        activities, key=lambda value: value[0]
    )
    if actor_id:
        actor_label = await db.scalar(
            select(User.display_name).where(User.user_id == actor_id)
        ) or actor_id
    return {
        "cycle_id": cycle.cycle_id,
        "billing_month": cycle.billing_month,
        "status": view["status"],
        "stored_status": view["stored_status"],
        "progress": view["progress"],
        "current_step": view["current_step"],
        "current_step_label": current["label"] if current else "已完成",
        "blocking_count": current["blocking_count"] if current else 0,
        "current_evidence_hash": current["evidence_hash"] if current else None,
        "blocking_issues": current["issues"] if current else [],
        "missing_source_count": len(missing_sources),
        "missing_sources": missing_sources,
        "inbox_pending_count": inbox_pending_count,
        "last_actor": actor_label or "系统",
        "updated_at": latest_at.isoformat(),
        "completed_at": view["completed_at"],
    }


async def build_lightweight_cycle_summaries(
    db: AsyncSession,
    cycles: list[MonthlyCloseCycle],
) -> dict[str, dict[str, Any]]:
    """Build completed and untouched-month rows with a fixed query count.

    Completed months already have an immutable final snapshot. The detailed
    month endpoint and the daily monitor still perform live evidence drift
    checks. A newly created month with no confirmation is also exactly
    representable from sources, documents, and inbox rows. The overview must
    not recalculate nine financial adapters for either case on every poll.
    """
    if not cycles:
        return {}
    cycle_ids = [cycle.cycle_id for cycle in cycles]
    requirements = list(
        (
            await db.execute(
                select(MonthlyCloseSourceRequirement).where(
                    MonthlyCloseSourceRequirement.cycle_id.in_(cycle_ids)
                )
            )
        ).scalars()
    )
    inbox_rows = list(
        (
            await db.execute(
                select(MonthlyCloseInboxItem).where(
                    MonthlyCloseInboxItem.cycle_id.in_(cycle_ids)
                )
            )
        ).scalars()
    )
    document_rows = list(
        (
            await db.execute(
                select(MonthlyCloseDocument).where(
                    MonthlyCloseDocument.cycle_id.in_(cycle_ids)
                )
            )
        ).scalars()
    )
    confirmation_rows = list(
        (
            await db.execute(
                select(MonthlyCloseStepConfirmation).where(
                    MonthlyCloseStepConfirmation.cycle_id.in_(cycle_ids)
                )
            )
        ).scalars()
    )

    requirements_by_cycle: dict[str, list[MonthlyCloseSourceRequirement]] = {}
    inbox_by_cycle: dict[str, list[MonthlyCloseInboxItem]] = {}
    documents_by_cycle: dict[str, list[MonthlyCloseDocument]] = {}
    confirmed_cycle_ids = {row.cycle_id for row in confirmation_rows}
    actor_ids: set[str] = set()
    for requirement in requirements:
        requirements_by_cycle.setdefault(requirement.cycle_id, []).append(requirement)
        if requirement.decided_by:
            actor_ids.add(requirement.decided_by)
    for item in inbox_rows:
        inbox_by_cycle.setdefault(item.cycle_id, []).append(item)
        if item.updated_by or item.created_by:
            actor_ids.add(item.updated_by or item.created_by)
    for document in document_rows:
        documents_by_cycle.setdefault(document.cycle_id, []).append(document)
        if document.invalidated_by or document.uploaded_by:
            actor_ids.add(document.invalidated_by or document.uploaded_by)
    for cycle in cycles:
        actor_ids.update(
            actor
            for actor in (
                cycle.created_by,
                cycle.completed_by,
                cycle.reopened_by,
            )
            if actor
        )
    actor_names = dict(
        (
            await db.execute(
                select(User.user_id, User.display_name).where(User.user_id.in_(actor_ids))
            )
        ).all()
    ) if actor_ids else {}

    result: dict[str, dict[str, Any]] = {}
    for cycle in cycles:
        if cycle.status != "completed" and cycle.cycle_id in confirmed_cycle_ids:
            continue
        activities: list[tuple[datetime, str | None, str | None]] = []

        def add_activity(
            at: datetime | None,
            actor_id: str | None,
            actor_label: str | None = None,
        ) -> None:
            if at is None:
                return
            normalized = at if at.tzinfo else at.replace(tzinfo=timezone.utc)
            activities.append((normalized, actor_id, actor_label))

        add_activity(cycle.created_at, cycle.created_by)
        add_activity(cycle.completed_at, cycle.completed_by)
        add_activity(cycle.reopened_at, cycle.reopened_by)
        cycle_requirements = requirements_by_cycle.get(cycle.cycle_id, [])
        for requirement in cycle_requirements:
            add_activity(requirement.decided_at, requirement.decided_by)
        cycle_inbox = inbox_by_cycle.get(cycle.cycle_id, [])
        for item in cycle_inbox:
            add_activity(
                item.updated_at,
                item.updated_by or item.created_by,
                item.submitted_label or (
                    "外部上传" if item.origin == "external" else None
                ),
            )
        for document in documents_by_cycle.get(cycle.cycle_id, []):
            add_activity(document.uploaded_at, document.uploaded_by)
            add_activity(document.invalidated_at, document.invalidated_by)
        latest_at, actor_id, actor_label = max(
            activities, key=lambda value: value[0]
        )
        if actor_id:
            actor_label = actor_names.get(actor_id, actor_id)
        missing_sources = [
            requirement.source_type
            for requirement in cycle_requirements
            if requirement.state == "pending"
        ]
        inbox_pending_count = sum(
            item.status not in {"confirmed", "dismissed"}
            for item in cycle_inbox
        )
        active_source_types = {
            document.source_type
            for document in documents_by_cycle.get(cycle.cycle_id, [])
            if document.is_active
        }
        source_document_missing_count = sum(
            requirement.state == "uploaded"
            and requirement.source_type not in active_source_types
            for requirement in cycle_requirements
        )
        is_completed = cycle.status == "completed"
        needs_recheck = (
            is_completed and cycle.monitor_effective_status == "needs_recheck"
        )
        cached_current_step = (
            cycle.monitor_current_step if needs_recheck else None
        )
        blocking_count = (
            int(cycle.monitor_blocking_count or 0)
            if needs_recheck
            else 0
            if is_completed
            else len(missing_sources)
            + source_document_missing_count
            + inbox_pending_count
        )
        result[cycle.cycle_id] = {
            "cycle_id": cycle.cycle_id,
            "billing_month": cycle.billing_month,
            "status": "needs_recheck" if needs_recheck else cycle.status,
            "stored_status": cycle.status,
            "progress": (
                int(cycle.monitor_progress or 0)
                if needs_recheck
                else 9 if is_completed else 0
            ),
            "current_step": (
                cached_current_step
                if needs_recheck
                else None if is_completed else "source_collection"
            ),
            "current_step_label": (
                STEP_LABELS.get(cached_current_step, "需要重新核对")
                if needs_recheck
                else "已完成" if is_completed else "资料收集"
            ),
            "blocking_count": blocking_count,
            "current_evidence_hash": None,
            "blocking_issues": [],
            "missing_source_count": len(missing_sources),
            "missing_sources": missing_sources,
            "inbox_pending_count": inbox_pending_count,
            "last_actor": actor_label or "系统",
            "updated_at": latest_at.isoformat(),
            "completed_at": (
                cycle.completed_at.isoformat() if cycle.completed_at else None
            ),
        }
    return result


async def confirm_step(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    step_key: str,
    user_id: str,
    *,
    expected_evidence_hash: str,
    note: str | None = None,
    evidence_loader: EvidenceLoader | None = None,
) -> MonthlyCloseStepConfirmation:
    if step_key not in MONTHLY_CLOSE_STEP_KEYS:
        raise MonthlyCloseConflict("unknown_step", "月结步骤不存在")
    locked = (
        await db.execute(
            select(MonthlyCloseCycle)
            .where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
            .with_for_update()
        )
    ).scalar_one()
    if locked.status == "completed":
        raise MonthlyCloseConflict(
            "cycle_completed", "本月月结已完成，如需修改请先填写原因重新打开"
        )

    loader = evidence_loader or build_step_evidences
    evidences = await loader(db, locked)
    evidence_by_key = {item.step_key: item for item in evidences}
    current = evidence_by_key[step_key]
    if expected_evidence_hash != current.evidence_hash:
        raise MonthlyCloseConflict(
            "evidence_changed",
            "页面数据已变化，请刷新后重新确认",
            detail={"current_evidence_hash": current.evidence_hash},
        )

    rows = list(
        (
            await db.execute(
                select(MonthlyCloseStepConfirmation)
                .where(MonthlyCloseStepConfirmation.cycle_id == locked.cycle_id)
                .with_for_update()
            )
        ).scalars()
    )
    confirmations = {item.step_key: item for item in rows}
    effective = _effective_steps(evidences, confirmations)
    position = MONTHLY_CLOSE_STEP_KEYS.index(step_key)
    if position > 0 and effective[position - 1]["status"] != "confirmed":
        previous_key = MONTHLY_CLOSE_STEP_KEYS[position - 1]
        raise MonthlyCloseConflict(
            "previous_step_incomplete",
            f"请先完成{STEP_LABELS[previous_key]}",
            detail={"previous_step": previous_key},
        )
    if current.blocking_count:
        raise MonthlyCloseConflict(
            "step_blocked",
            "当前步骤仍有未处理项，不能确认",
            detail={
                "step_key": step_key,
                "blocking_count": current.blocking_count,
                "issues": current.snapshot["issues"],
            },
        )

    now = datetime.now(timezone.utc)
    confirmation = confirmations.get(step_key)
    if confirmation is not None and confirmation.evidence_hash == current.evidence_hash:
        return confirmation
    if confirmation is None:
        confirmation = MonthlyCloseStepConfirmation(
            confirmation_id="MCC-" + uuid4().hex[:12].upper(),
            cycle_id=locked.cycle_id,
            step_key=step_key,
            evidence_hash=current.evidence_hash,
            evidence=current.snapshot,
            history=[],
            note=note,
            confirmed_by=user_id,
            confirmed_at=now,
        )
        db.add(confirmation)
    else:
        if (confirmation.evidence or {}).get("_state") != "reopen_invalidated":
            previous = {
                "evidence_hash": confirmation.evidence_hash,
                "evidence": confirmation.evidence,
                "note": confirmation.note,
                "confirmed_by": confirmation.confirmed_by,
                "confirmed_at": confirmation.confirmed_at.isoformat(),
            }
            confirmation.history = [*(confirmation.history or []), previous]
        confirmation.evidence_hash = current.evidence_hash
        confirmation.evidence = current.snapshot
        confirmation.note = note
        confirmation.confirmed_by = user_id
        confirmation.confirmed_at = now

    if step_key == MONTHLY_CLOSE_STEP_KEYS[-1]:
        locked.status = "completed"
        locked.completed_by = user_id
        locked.completed_at = now
        locked.monitor_effective_status = "completed"
        locked.monitor_current_step = None
        locked.monitor_progress = 9
        locked.monitor_blocking_count = 0
        locked.final_snapshot = {
            "billing_month": locked.billing_month,
            "steps": [
                {
                    "step_key": item.step_key,
                    "evidence_hash": item.evidence_hash,
                    "evidence": item.snapshot,
                }
                for item in evidences
            ],
        }
    await log_action_tx(
        db,
        user_id,
        "monthly_close.step.confirm",
        "monthly_close",
        locked.cycle_id,
        after_data={
            "billing_month": locked.billing_month,
            "step_key": step_key,
            "evidence_hash": current.evidence_hash,
        },
    )
    await db.commit()
    await db.refresh(confirmation)
    return confirmation


async def reopen_cycle(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    user_id: str,
    *,
    reason: str,
) -> MonthlyCloseCycle:
    reason = reason.strip()
    if not reason:
        raise MonthlyCloseConflict(
            "reopen_reason_required", "重新打开已完成月结必须填写原因"
        )
    locked = (
        await db.execute(
            select(MonthlyCloseCycle)
            .where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
            .with_for_update()
        )
    ).scalar_one()
    if locked.status != "completed":
        raise MonthlyCloseConflict("cycle_not_completed", "只有已完成月结可以重新打开")
    now = datetime.now(timezone.utc)
    locked.status = "reopened"
    locked.reopened_by = user_id
    locked.reopened_at = now
    locked.reopen_reason = reason
    locked.completed_by = None
    locked.completed_at = None
    locked.monitor_effective_status = "reopened"
    locked.monitor_current_step = "source_collection"
    locked.monitor_progress = 0
    locked.monitor_blocking_count = 0
    confirmations = list(
        (
            await db.execute(
                select(MonthlyCloseStepConfirmation)
                .where(MonthlyCloseStepConfirmation.cycle_id == locked.cycle_id)
                .with_for_update()
            )
        ).scalars()
    )
    for confirmation in confirmations:
        previous_hash = confirmation.evidence_hash
        confirmation.history = [
            *(confirmation.history or []),
            {
                "evidence_hash": previous_hash,
                "evidence": confirmation.evidence,
                "note": confirmation.note,
                "confirmed_by": confirmation.confirmed_by,
                "confirmed_at": confirmation.confirmed_at.isoformat(),
            },
        ]
        confirmation.evidence_hash = hashlib.sha256(
            f"reopened:{locked.cycle_id}:{now.isoformat()}:{confirmation.step_key}".encode()
        ).hexdigest()
        confirmation.evidence = {
            "_state": "reopen_invalidated",
            "reason": reason,
            "previous_evidence_hash": previous_hash,
        }
    await log_action_tx(
        db,
        user_id,
        "monthly_close.reopen",
        "monthly_close",
        locked.cycle_id,
        before_data={"status": "completed"},
        after_data={"status": "reopened", "reason": reason},
    )
    await db.commit()
    await db.refresh(locked)
    return locked
