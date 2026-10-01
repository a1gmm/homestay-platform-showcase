"""Administrator-confirmed, date-only historical records with no operational side effects."""

import json
from datetime import date
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.cleaning_work_record import CleaningWorkImport, CleaningWorkRecord
from app.models.monthly_close import MonthlyCloseCycle, MonthlyCloseDocument
from app.models.room import Room
from app.models.user import User
from app.services.audit import log_action_tx
from app.services.monthly_close.cleaning_work_log import (
    compare_cleaning_work_log,
    parse_cleaning_work_log,
)
from app.services.monthly_close.service_reconciliation import (
    _resolve_room_reference,
    _room_alias_index,
)
from app.services.monthly_close.workflow import (
    MonthlyCloseConflict,
    require_cycle_writable,
)


async def _admin(db, actor_id, *, lock=False):
    query = (
        select(User)
        .where(User.user_id == actor_id)
        .execution_options(populate_existing=True)
    )
    actor = await db.scalar(query.with_for_update() if lock else query)
    if actor is None or not actor.is_active or actor.role.value != "admin":
        raise MonthlyCloseConflict(
            "work_log_admin_required", "只有当前有效管理员可以补齐历史打扫记录"
        )


async def preview_work_import(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    document: MonthlyCloseDocument,
    actor_id: str,
) -> dict:
    await _admin(db, actor_id)
    if cycle.status == "completed":
        raise MonthlyCloseConflict("cycle_completed", "本月已关账，请先重新打开月结")
    if (
        document.cycle_id != cycle.cycle_id
        or not document.is_active
        or document.source_type != "cleaning_statement"
    ):
        raise MonthlyCloseConflict("document_not_found", "有效的保洁工作记录不存在")
    if sha256(document.content).hexdigest() != document.sha256:
        raise MonthlyCloseConflict(
            "document_hash_changed", "原文件校验失败，请重新核实文件"
        )
    entries = parse_cleaning_work_log(
        document.content, document.filename, cycle.billing_month
    )
    if entries is None:
        raise MonthlyCloseConflict(
            "not_cleaning_work_log", "该文件不是按天填写房间的保洁工作记录"
        )
    comparison = await compare_cleaning_work_log(
        db, entries, cycle.billing_month, document.document_id
    )
    aliases = _room_alias_index(
        list((await db.execute(select(Room.room_id, Room.room_name))).tuples())
    )
    evidence = {}
    for entry in entries:
        room, _, _ = _resolve_room_reference(entry.room_ref, aliases)
        evidence[(entry.service_date, room, entry.service_type)] = entry
    actions = []
    for item in comparison.differences:
        if item.status not in {"table_only", "not_completed"} or item.table_count != 1:
            continue
        entry = evidence[(item.service_date, item.room_ref, item.service_type)]
        actions.append(
            {
                "service_date": item.service_date,
                "room_ref": item.room_ref,
                "service_type": item.service_type,
                "source_sheet": entry.source_sheet,
                "source_row": entry.source_row,
                "action": "add_history_record",
            }
        )
    bound = {
        "policy": "cleaning_work_import_v1",
        "actor_id": actor_id,
        "cycle_id": cycle.cycle_id,
        "document_id": document.document_id,
        "document_sha256": document.sha256,
        "comparison": comparison.model_dump(),
        "actions": actions,
    }
    fingerprint = sha256(
        json.dumps(
            bound, sort_keys=True, ensure_ascii=False, separators=(",", ":")
        ).encode()
    ).hexdigest()
    return {
        "preview_hash": fingerprint,
        "document_id": document.document_id,
        "billing_month": cycle.billing_month,
        "actions": actions,
        "add_count": len(actions),
        "remaining_count": len(comparison.differences) - len(actions),
        "comparison": comparison.model_dump(),
        "effect": "新增有原表依据的历史打扫记录；当前任务、房态和费用保持不变。",
    }


async def confirm_work_import(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    document: MonthlyCloseDocument,
    actor_id: str,
    preview_hash: str,
    request_id: str,
) -> dict:
    cycle = await require_cycle_writable(db, cycle)
    await _admin(db, actor_id, lock=True)
    # Refresh after obtaining the cycle lock: concurrent imports/removals may have committed.
    await db.refresh(cycle)
    await db.refresh(
        document,
        with_for_update=True,
        attribute_names=[
            "cycle_id",
            "is_active",
            "source_type",
            "content",
            "sha256",
            "filename",
        ],
    )
    existing = await db.scalar(
        select(CleaningWorkImport).where(
            CleaningWorkImport.cycle_id == cycle.cycle_id,
            CleaningWorkImport.request_id == request_id,
        )
    )
    if existing is not None:
        if (
            existing.document_id != document.document_id
            or existing.created_by != actor_id
            or existing.preview_hash != preview_hash
        ):
            raise MonthlyCloseConflict(
                "work_log_request_conflict", "该确认请求已用于其他预览，请重新预览"
            )
        return existing.result
    fresh = await preview_work_import(db, cycle, document, actor_id)
    if fresh["preview_hash"] != preview_hash:
        raise MonthlyCloseConflict(
            "work_log_preview_stale", "文件或系统打扫记录已变化，请重新预览后确认"
        )
    if not fresh["actions"]:
        raise MonthlyCloseConflict(
            "work_log_nothing_to_add", "当前没有可自动补齐的记录，请处理剩余待核实项"
        )
    batch = CleaningWorkImport(
        import_id=f"CWI-{uuid4().hex[:20]}",
        cycle_id=cycle.cycle_id,
        document_id=document.document_id,
        request_id=request_id,
        preview_hash=preview_hash,
        created_by=actor_id,
        result={},
    )
    db.add(batch)
    await db.flush()
    ids = []
    for action in fresh["actions"]:
        record = CleaningWorkRecord(
            record_id=f"CWR-{uuid4().hex[:20]}",
            import_id=batch.import_id,
            room_id=action["room_ref"],
            service_date=date.fromisoformat(action["service_date"]),
            service_type=action["service_type"],
            document_id=document.document_id,
            document_sha256=document.sha256,
            source_sheet=action["source_sheet"],
            source_row=action["source_row"],
            created_by=actor_id,
        )
        db.add(record)
        ids.append(record.record_id)
    await db.flush()
    entries = parse_cleaning_work_log(
        document.content, document.filename, cycle.billing_month
    )
    after = await compare_cleaning_work_log(
        db, entries, cycle.billing_month, document.document_id
    )
    result = {
        "import_id": batch.import_id,
        "added_count": len(ids),
        "record_ids": ids,
        "comparison": after.model_dump(),
    }
    batch.result = result
    await log_action_tx(
        db,
        actor_id,
        "monthly_close.cleaning_work_records.import",
        "monthly_close_document",
        document.document_id,
        after_data={
            "cycle_id": cycle.cycle_id,
            "import_id": batch.import_id,
            "preview_hash": preview_hash,
            "record_ids": ids,
            "added_count": len(ids),
            "remaining_count": len(after.differences),
        },
    )
    await db.commit()
    return result
