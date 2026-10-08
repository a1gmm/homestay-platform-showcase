"""Compare daily cleaning activity evidence without deriving charges or changing tasks."""

from __future__ import annotations

import json
import re
from collections import defaultdict
from datetime import date, datetime, time, timezone
from hashlib import sha256

from pydantic import BaseModel, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_helpers import CN_TZ
from app.models.cleaning_request import CleaningRequest, CleaningRequestStatus
from app.models.cleaning_work_record import CleaningWorkRecord
from app.models.cleaning_work_resolution import CleaningWorkResolution
from app.models.monthly_close import MonthlyCloseDocument
from app.models.room import Room
from app.models.task import Task, TaskStatus, TaskType
from app.models.user import User
from app.services.billing_recon.parser import load_workbook_rows
from app.services.monthly_close.service_reconciliation import (
    _resolve_room_reference,
    _room_alias_index,
)
from app.services.monthly_close.service_statement import (
    ServiceStatementError,
    _date,
    _text,
)

LOCAL_TIME = CN_TZ
MAX_RECORDS = 5000
NO_SERVICE_MARKERS = {"", "无", "0", "-", "—", "休班", "上午休班", "下午休班", "休息"}


class WorkLogEntry(BaseModel):
    service_date: str
    room_ref: str
    service_type: str
    source_sheet: str
    source_row: int


class WorkLogDifference(BaseModel):
    service_date: str
    room_ref: str
    service_type: str
    status: str
    source_rows: list[int] = Field(default_factory=list)
    system_ids: list[str] = Field(default_factory=list)
    table_count: int = 0
    system_count: int = 0
    evidence_hash: str = ""
    source_entries: list[dict] = Field(default_factory=list)
    system_evidence: list[dict] = Field(default_factory=list)


class WorkLogComparison(BaseModel):
    record_count: int
    normal_count: int
    instay_count: int
    matched_count: int = 0
    effective_record_count: int = 0
    resolutions: list[dict] = Field(default_factory=list)
    recorded_count: int = 0
    importable_count: int = 0
    records: list[dict] = Field(default_factory=list)
    differences: list[WorkLogDifference] = Field(default_factory=list)
    basis: str = "以表格的日期、房间和打扫类型为核对依据；忽略超出数量、续住打扫次数及金额。同格简写沿用前一个完整四位房号的前两位，例如1607.08读取为1607、1608。"
    note: str = "正常打扫按系统完成时间（北京时间）核对，续住打扫按申请日期及完成状态核对。管理员确认的历史打扫记录也作为完成依据；重复项和表外记录仍需单独核实。"


def parse_cleaning_work_log(
    data: bytes, filename: str, billing_month: str | None
) -> list[WorkLogEntry] | None:
    sheets, datemode = load_workbook_rows(data, filename)
    candidates = []
    for sheet, rows in sheets.items():
        for index, row in enumerate(rows[:30]):
            headers = ["".join(str(cell or "").split()) for cell in row]
            if {"日期", "正常打扫房间号", "续住房间"} <= set(headers):
                candidates.append(
                    (
                        sheet,
                        rows,
                        index,
                        headers.index("日期"),
                        headers.index("正常打扫房间号"),
                        headers.index("续住房间"),
                    )
                )
                break
    if not candidates:
        return None
    entries = []
    for sheet, rows, header, date_col, normal_col, instay_col in candidates:
        current_date = None
        for index, row in enumerate(rows[header + 1 :], start=header + 2):
            cell = lambda col, row=row: row[col] if col < len(row) else None
            raw_date = _text(cell(date_col))
            rooms_present = any(
                _text(cell(col)) not in NO_SERVICE_MARKERS
                for col in (normal_col, instay_col)
            )
            if raw_date in {"合计", "总计", "小计"}:
                current_date = None
                if rooms_present:
                    raise ServiceStatementError(
                        f"{sheet}第{index}行汇总中含有房间，请核对原表"
                    )
                continue
            if not raw_date and not rooms_present:
                current_date = None
                continue
            if raw_date:
                try:
                    current_date = _date(cell(date_col), index, datemode, billing_month)
                except ServiceStatementError as exc:
                    if not rooms_present:
                        # Monthly templates often retain an unused 31st day.
                        # Never carry a prior date across an invalid empty row.
                        current_date = None
                        continue
                    raise ServiceStatementError(f"{sheet}：{exc}") from exc
                if billing_month and current_date.strftime("%Y-%m") != billing_month:
                    raise ServiceStatementError(
                        f"{sheet}第{index}行日期不属于{billing_month}"
                    )
            if not rooms_present:
                continue
            if current_date is None:
                raise ServiceStatementError(
                    f"{sheet}第{index}行缺少日期，无法确定房间属于哪一天"
                )
            for col, kind in (
                (normal_col, "cleaning"),
                (instay_col, "instay_cleaning"),
            ):
                raw = _text(cell(col))
                if raw in NO_SERVICE_MARKERS:
                    continue
                # Never expand ranges or discard suffixes: unresolved references remain visible.
                prefix = None
                for room in filter(None, re.split(r"[.．。、、,，;；/\s]+", raw)):
                    if room in NO_SERVICE_MARKERS:
                        continue
                    if re.fullmatch(r"[0-9]{4}", room):
                        prefix = room[:2]
                    elif re.fullmatch(r"[0-9]{2}", room) and prefix:
                        room = prefix + room
                    else:
                        # Never carry a floor across an unrelated or unresolved token.
                        prefix = None
                    if len(room) > 100:
                        raise ServiceStatementError(
                            f"{sheet}第{index}行房间标记过长，请核对原表"
                        )
                    entries.append(
                        WorkLogEntry(
                            service_date=current_date.isoformat(),
                            room_ref=room,
                            service_type=kind,
                            source_sheet=sheet,
                            source_row=index,
                        )
                    )
                    if len(entries) > MAX_RECORDS:
                        raise ServiceStatementError(
                            "保洁工作记录超过5000条，请拆分文件后核对"
                        )
    if not entries:
        raise ServiceStatementError("工作记录中没有正常打扫或续住房间")
    return entries


def _local_date(value: datetime) -> date:
    # Production timestamps are timezone aware; SQLite drops the UTC offset in tests.
    return (
        (value if value.tzinfo else value.replace(tzinfo=timezone.utc))
        .astimezone(LOCAL_TIME)
        .date()
    )


def _utc_text(value):
    if value is None:
        return None
    return (
        (value if value.tzinfo else value.replace(tzinfo=timezone.utc))
        .astimezone(timezone.utc)
        .isoformat()
    )


async def compare_cleaning_work_log(
    db: AsyncSession,
    entries: list[WorkLogEntry],
    billing_month: str,
    document_id: str | None = None,
    *,
    apply_resolutions: bool = True,
) -> WorkLogComparison:
    year, month = map(int, billing_month.split("-"))
    first = date(year, month, 1)
    last = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    start = datetime.combine(first, time.min, LOCAL_TIME).astimezone(timezone.utc)
    end = datetime.combine(last, time.min, LOCAL_TIME).astimezone(timezone.utc)
    rooms = list((await db.execute(select(Room.room_id, Room.room_name))).tuples())
    aliases = _room_alias_index(rooms)
    known_rooms = {room for room, _ in rooms}
    tasks = list(
        await db.scalars(
            select(Task)
            .execution_options(populate_existing=True)
            .where(
                Task.task_type == TaskType.cleaning,
                Task.status != TaskStatus.cancelled,
                or_(
                    (Task.completed_at >= start) & (Task.completed_at < end),
                    (Task.deadline >= start) & (Task.deadline < end),
                ),
            )
            .limit(MAX_RECORDS + 1)
        )
    )
    requests = list(
        await db.scalars(
            select(CleaningRequest)
            .execution_options(populate_existing=True)
            .where(
                CleaningRequest.request_date >= first,
                CleaningRequest.request_date < last,
            )
            .limit(MAX_RECORDS + 1)
        )
    )
    if len(tasks) > MAX_RECORDS or len(requests) > MAX_RECORDS:
        raise ServiceStatementError("系统打扫记录超过本次核对上限，未返回部分结论")
    system = defaultdict(list)
    operational_details = {}
    for task in tasks:
        operational_details[task.task_id] = {
            "status": task.status.value,
            "completed_at": _utc_text(task.completed_at) if task.completed_at else None,
            "deadline": _utc_text(task.deadline) if task.deadline else None,
        }
        actual = task.completed_at or task.deadline
        if not actual or not task.room_id:
            continue
        day = _local_date(actual)
        if first <= day < last:
            system[(day.isoformat(), task.room_id, "cleaning")].append(
                (
                    task.task_id,
                    task.status == TaskStatus.done and task.completed_at is not None,
                )
            )
    for request in requests:
        operational_details[request.request_id] = {
            "status": request.status.value,
            "request_date": request.request_date.isoformat(),
        }
        system[
            (request.request_date.isoformat(), request.room_id, "instay_cleaning")
        ].append((request.request_id, request.status == CleaningRequestStatus.cleaned))
    records = list(
        await db.scalars(
            select(CleaningWorkRecord)
            .where(
                CleaningWorkRecord.service_date >= first,
                CleaningWorkRecord.service_date < last,
            )
            .order_by(CleaningWorkRecord.service_date, CleaningWorkRecord.room_id)
            .order_by(CleaningWorkRecord.record_id)
            .limit(MAX_RECORDS + 1)
        )
    )
    if len(records) > MAX_RECORDS:
        raise ServiceStatementError("历史打扫记录超过本次核对上限，未返回部分结论")
    native = {key: list(value) for key, value in system.items()}
    histories = {}
    for record in records:
        key = (record.service_date.isoformat(), record.room_id, record.service_type)
        histories[key] = record
        # A confirmed history entry provides completion evidence for the same
        # event, not an additional visit alongside an operational task.
        if record.quantity > len(system[key]) or (
            len(system[key]) == 1 and not system[key][0][1]
        ):
            system[key] = [(record.record_id, True)] * record.quantity
    result = WorkLogComparison(
        recorded_count=sum(record.quantity for record in records),
        effective_record_count=len(entries),
        records=[
            {
                "record_id": record.record_id,
                "quantity": record.quantity,
                "service_date": record.service_date.isoformat(),
                "room_ref": record.room_id,
                "service_type": record.service_type,
                "source_sheet": record.source_sheet,
                "source_row": record.source_row,
                "document_id": record.document_id,
            }
            for record in records
        ],
        record_count=len(entries),
        normal_count=sum(entry.service_type == "cleaning" for entry in entries),
        instay_count=sum(entry.service_type == "instay_cleaning" for entry in entries),
    )
    table = defaultdict(list)
    for entry in entries:
        room_id, candidates, _ = _resolve_room_reference(entry.room_ref, aliases)
        if room_id not in known_rooms or len(candidates) > 1:
            result.differences.append(
                WorkLogDifference(
                    service_date=entry.service_date,
                    room_ref=entry.room_ref,
                    service_type=entry.service_type,
                    status="unknown_room",
                    source_rows=[entry.source_row],
                    table_count=1,
                )
            )
            continue
        table[(entry.service_date, room_id, entry.service_type)].append(entry)
    resolutions = {}
    document_sha = None
    if document_id:
        document_sha = await db.scalar(
            select(MonthlyCloseDocument.sha256).where(
                MonthlyCloseDocument.document_id == document_id
            )
        )
        rows = await db.execute(
            select(CleaningWorkResolution, User.display_name)
            .join(User, User.user_id == CleaningWorkResolution.confirmed_by)
            .where(CleaningWorkResolution.document_id == document_id)
            .order_by(
                CleaningWorkResolution.created_at, CleaningWorkResolution.resolution_id
            )
        )
        for resolution, name in rows:
            resolutions[
                (
                    resolution.service_date.isoformat(),
                    resolution.room_id,
                    resolution.service_type,
                )
            ] = (resolution, name)
    for key in sorted(
        set(table) | set(system) | (set(resolutions) if apply_resolutions else set())
    ):
        source, found = table[key], system[key]
        source_entries = [entry.model_dump() for entry in source]
        system_evidence = [
            {
                "record_id": item_id,
                "completed": complete,
                "kind": "operational",
                **operational_details[item_id],
            }
            for item_id, complete in sorted(native.get(key, []))
        ]
        history = histories.get(key)
        if history:
            system_evidence.append(
                {
                    "record_id": history.record_id,
                    "completed": True,
                    "kind": "history",
                    "quantity": history.quantity,
                    "document_id": history.document_id,
                    "document_sha256": history.document_sha256,
                }
            )
        evidence_hash = sha256(
            json.dumps(
                {
                    "document_sha256": document_sha,
                    "key": key,
                    "source": source_entries,
                    "system": system_evidence,
                },
                ensure_ascii=False,
                sort_keys=True,
            ).encode()
        ).hexdigest()
        if apply_resolutions and key in resolutions:
            resolution, actor_name = resolutions[key]
            valid = resolution.evidence_hash == evidence_hash
            result.resolutions.append(
                {
                    "resolution_id": resolution.resolution_id,
                    "service_date": key[0],
                    "room_ref": key[1],
                    "service_type": key[2],
                    "decision": resolution.decision,
                    "confirmed_count": resolution.confirmed_count,
                    "reason": resolution.reason,
                    "confirmed_by": actor_name or resolution.confirmed_by,
                    "created_at": resolution.created_at.isoformat(),
                    "state": "applied" if valid else "stale",
                    "evidence": resolution.evidence,
                }
            )
            if valid:
                result.effective_record_count += resolution.confirmed_count - len(
                    source
                )
                result.matched_count += resolution.confirmed_count
                continue
        if not source and not found:
            continue
        if len(source) == len(found) == 1 and found[0][1]:
            result.matched_count += 1
            continue
        status = (
            "duplicate"
            if len(source) > 1 or len(found) > 1
            else "table_only"
            if not found
            else "system_only"
            if not source
            else "not_completed"
        )
        result.differences.append(
            WorkLogDifference(
                service_date=key[0],
                room_ref=key[1],
                service_type=key[2],
                status=status,
                evidence_hash=evidence_hash,
                source_entries=source_entries,
                system_evidence=system_evidence,
                source_rows=[entry.source_row for entry in source],
                system_ids=[record[0] for record in found],
                table_count=len(source),
                system_count=len(found),
            )
        )
    result.importable_count = sum(
        item.status in {"table_only", "not_completed"} and item.table_count == 1
        for item in result.differences
    )
    return result
