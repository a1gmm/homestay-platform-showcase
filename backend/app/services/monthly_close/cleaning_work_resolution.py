"""Evidence-bound administrator decisions for ambiguous cleaning visits."""

import json
from datetime import date
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select

from app.models.cleaning_work_record import CleaningWorkImport, CleaningWorkRecord
from app.models.cleaning_work_resolution import CleaningWorkResolution
from app.services.audit import log_action_tx
from app.services.monthly_close.cleaning_work_import import _admin, preview_work_import
from app.services.monthly_close.cleaning_work_log import (
    compare_cleaning_work_log,
    parse_cleaning_work_log,
)
from app.services.monthly_close.workflow import (
    MonthlyCloseConflict,
    require_cycle_writable,
)


async def preview_work_resolution(db, cycle, document, actor_id, selection):
    report = (await preview_work_import(db, cycle, document, actor_id))["comparison"]
    reason = selection["reason"].strip()
    if not 2 <= len(reason) <= 1000:
        raise MonthlyCloseConflict(
            "resolution_reason_required", "请填写至少两个字的核实说明"
        )
    item = next(
        (
            item
            for item in report["differences"]
            if all(
                item[key] == selection[key]
                for key in ("service_date", "room_ref", "service_type")
            )
        ),
        None,
    )
    if not item or item["status"] not in {"duplicate", "system_only"}:
        raise MonthlyCloseConflict(
            "resolution_item_changed", "该差异已变化或不支持此处理方式，请刷新核对结果"
        )
    decision = selection["decision"]
    allowed = (
        {"exclude_system"}
        if item["table_count"] == 0
        else {"accept_table"} | ({"count_once"} if item["table_count"] > 1 else set())
    )
    if decision not in allowed:
        raise MonthlyCloseConflict(
            "resolution_decision_invalid", "处理方式与当前原表记录不符"
        )
    count = (
        0
        if decision == "exclude_system"
        else 1
        if decision == "count_once"
        else item["table_count"]
    )
    bound = {
        "policy": "cleaning_resolution_v1",
        "actor_id": actor_id,
        "cycle_id": cycle.cycle_id,
        "document_id": document.document_id,
        "document_sha256": document.sha256,
        "item": item,
        "selection": {**selection, "reason": reason},
        "confirmed_count": count,
    }
    return {
        "preview_hash": sha256(
            json.dumps(bound, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest(),
        "selection": bound["selection"],
        "item": item,
        "confirmed_count": count,
        "comparison": report,
    }


async def confirm_work_resolution(
    db, cycle, document, actor_id, selection, preview_hash, request_id, *, commit=True
):
    cycle = await require_cycle_writable(db, cycle)
    await _admin(db, actor_id, lock=True)
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
    normalized = {**selection, "reason": selection["reason"].strip()}
    if existing:
        if (
            existing.document_id != document.document_id
            or existing.created_by != actor_id
            or existing.preview_hash != preview_hash
            or existing.result.get("selection") != normalized
        ):
            raise MonthlyCloseConflict(
                "resolution_request_conflict", "该请求已用于其他处理，请重新预览"
            )
        return existing.result
    preview = await preview_work_resolution(db, cycle, document, actor_id, selection)
    if preview["preview_hash"] != preview_hash:
        raise MonthlyCloseConflict(
            "resolution_preview_stale", "原表或系统记录已变化，请重新预览后确认"
        )
    item, count = preview["item"], preview["confirmed_count"]
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
    record = await db.scalar(
        select(CleaningWorkRecord).where(
            CleaningWorkRecord.room_id == selection["room_ref"],
            CleaningWorkRecord.service_date
            == date.fromisoformat(selection["service_date"]),
            CleaningWorkRecord.service_type == selection["service_type"],
        )
    )
    before_quantity = record.quantity if record else 0
    # Never erase earlier evidence. A lower count applies only to this document's
    # reconciliation; additional confirmed visits become historical evidence.
    if count > before_quantity:
        if record:
            record.quantity = count
        else:
            source = item["source_entries"][0]
            record = CleaningWorkRecord(
                record_id=f"CWR-{uuid4().hex[:20]}",
                import_id=batch.import_id,
                room_id=selection["room_ref"],
                service_date=date.fromisoformat(selection["service_date"]),
                service_type=selection["service_type"],
                document_id=document.document_id,
                document_sha256=document.sha256,
                source_sheet=source["source_sheet"],
                source_row=source["source_row"],
                created_by=actor_id,
                quantity=count,
            )
            db.add(record)
    await db.flush()
    entries = parse_cleaning_work_log(
        document.content, document.filename, cycle.billing_month
    )
    raw = await compare_cleaning_work_log(
        db, entries, cycle.billing_month, document.document_id, apply_resolutions=False
    )
    post_item = next(
        (
            entry
            for entry in raw.differences
            if all(
                getattr(entry, key) == selection[key]
                for key in ("service_date", "room_ref", "service_type")
            )
        ),
        None,
    )
    if post_item is None:
        raise MonthlyCloseConflict(
            "resolution_evidence_changed", "保存期间差异已变化，请重新预览"
        )
    native_before = [
        row for row in item["system_evidence"] if row["kind"] == "operational"
    ]
    native_after = [
        row for row in post_item.system_evidence if row["kind"] == "operational"
    ]
    if (
        native_before != native_after
        or item["source_entries"] != post_item.source_entries
    ):
        raise MonthlyCloseConflict(
            "resolution_evidence_changed", "保存期间系统记录已变化，请重新预览"
        )
    resolution = CleaningWorkResolution(
        resolution_id=f"CWS-{uuid4().hex[:20]}",
        import_id=batch.import_id,
        document_id=document.document_id,
        service_date=date.fromisoformat(selection["service_date"]),
        room_id=selection["room_ref"],
        service_type=selection["service_type"],
        evidence_hash=post_item.evidence_hash,
        decision=selection["decision"],
        confirmed_count=count,
        reason=normalized["reason"],
        evidence={
            "before": item,
            "history_quantity_before": before_quantity,
            "history_quantity_after": record.quantity if record else 0,
            "record_id": record.record_id if record else None,
        },
        confirmed_by=actor_id,
    )
    db.add(resolution)
    await db.flush()
    after = await compare_cleaning_work_log(
        db, entries, cycle.billing_month, document.document_id
    )
    result = {
        "resolution_id": resolution.resolution_id,
        "selection": normalized,
        "confirmed_count": count,
        "comparison": after.model_dump(),
    }
    batch.result = result
    await log_action_tx(
        db,
        actor_id,
        "monthly_close.cleaning_work_records.resolve",
        "monthly_close_document",
        document.document_id,
        after_data={
            "resolution_id": resolution.resolution_id,
            "decision": selection["decision"],
            "reason": normalized["reason"],
            "confirmed_count": count,
            "evidence": resolution.evidence,
        },
    )
    if commit:
        await db.commit()
    return result
