"""Conversation plans backed by live records; only explicit confirmations mutate data."""

import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum
from hashlib import sha256
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import Date, DateTime, select
from sqlalchemy.orm import undefer

from app.core.datetime_helpers import today_cn
from app.models.cleaning_request import CleaningRequest
from app.models.cleaning_work_record import CleaningWorkImport, CleaningWorkRecord
from app.models.cleaning_work_removal import CleaningWorkRemoval
from app.models.expense import Expense, ExpenseCategory
from app.models.monthly_close import MonthlyCloseDocument
from app.models.monthly_close_control import MonthlyCloseRun
from app.models.task import Task
from app.services.audit import log_action_tx
from app.services.monthly_close.cleaning_work_import import (
    _admin,
    preview_work_import,
)
from app.services.monthly_close.cleaning_work_log import (
    WorkLogComparison,
    compare_cleaning_work_log,
    parse_cleaning_work_log,
)
from app.services.monthly_close.cleaning_work_resolution import (
    confirm_work_resolution,
    preview_work_resolution,
)
from app.services.monthly_close.workflow import (
    MonthlyCloseConflict,
    require_cycle_writable,
)

VERSION = "monthly-close-cleaning-chat/v1"
MODELS = {"task": Task, "request": CleaningRequest, "history": CleaningWorkRecord}


class ChatSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")
    add_missing: bool = False
    repair_fees: bool = False
    delete_extra: bool = False
    duplicate: Literal["ask", "once", "table"] = "ask"
    rooms: list[str] = Field(default_factory=list, max_length=100)
    service_date: str | None = None
    service_type: Literal["cleaning", "instay_cleaning"] | None = None
    restore_ids: list[str] = Field(default_factory=list, max_length=100)


class WorkChatFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["cleaning_work_chat"] = "cleaning_work_chat"
    document_id: str | None = None
    filename: str | None = None
    billing_month: str
    state: Literal["report", "proposal", "completed", "clarification", "conflict", "cancelled"]
    message: str
    comparison: WorkLogComparison | None = None
    spec: ChatSpec | None = None
    preview_hash: str | None = None
    actions: list[dict] = Field(default_factory=list)
    removal_ids: list[str] = Field(default_factory=list)
    reason: str = ""
    request_text: str = ""
    fee_summary: dict | None = None


def digest(value):
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def snapshot(record):
    result = {}
    for column in record.__table__.columns:
        value = getattr(record, column.key)
        if isinstance(value, Enum):
            value = value.value
        elif isinstance(value, datetime):
            value = (
                (value if value.tzinfo else value.replace(tzinfo=timezone.utc))
                .astimezone(timezone.utc)
                .isoformat()
            )
        elif isinstance(value, (date, Decimal)):
            value = str(value)
        result[column.key] = value
    return result


def reconstruct(model, data):
    values = dict(data)
    for column in model.__table__.columns:
        if values.get(column.key) is not None:
            if isinstance(column.type, DateTime):
                values[column.key] = datetime.fromisoformat(values[column.key])
            elif isinstance(column.type, Date):
                values[column.key] = date.fromisoformat(values[column.key])
    return model(**values)


def selected(row, spec):
    return (
        (not spec.rooms or row["room_ref"] in spec.rooms)
        and (not spec.service_date or row["service_date"] == spec.service_date)
        and (not spec.service_type or row["service_type"] == spec.service_type)
    )


async def read_report(db, cycle, document, actor_id):
    preview = await preview_work_import(db, cycle, document, actor_id)
    entries = parse_cleaning_work_log(
        document.content, document.filename, cycle.billing_month
    )
    raw = await compare_cleaning_work_log(
        db, entries, cycle.billing_month, document.document_id, apply_resolutions=False
    )
    return preview, raw


async def build_plan(db, cycle, document, actor_id, spec, reason):
    preview, raw = await read_report(db, cycle, document, actor_id)
    if spec.repair_fees:
        from app.services.monthly_close.work_record_fees import plan_work_fees
        if spec.add_missing or spec.delete_extra or spec.restore_ids or spec.duplicate != "ask" or spec.service_type == "cleaning":
            raise MonthlyCloseConflict("fee_plan_mixed", "请先完成打扫记录核对，再单独核对续住保洁费用；退房服务费需按关联订单核对")
        if preview["comparison"]["differences"]:
            raise MonthlyCloseConflict("work_not_reconciled", "打扫记录还有差异，请先核实次数，再补记费用")
        fees = await plan_work_fees(db, document, cycle.billing_month, rooms=spec.rooms, service_date=spec.service_date)
        return {"actions": fees["actions"], "fee_summary": fees,
                "comparison": preview["comparison"],
                "preview_hash": digest({"actor": actor_id, "cycle": cycle.cycle_id,
                    "document": document.sha256, "spec": spec.model_dump(), "reason": reason,
                    "comparison": preview["comparison"], "fees": fees})}
    actions = []
    if spec.restore_ids:
        removals = list(
            await db.scalars(
                select(CleaningWorkRemoval).where(
                    CleaningWorkRemoval.removal_id.in_(spec.restore_ids),
                    CleaningWorkRemoval.document_id == document.document_id,
                    CleaningWorkRemoval.restored_at.is_(None),
                )
            )
        )
        if len(removals) != len(set(spec.restore_ids)):
            raise MonthlyCloseConflict(
                "restore_changed",
                "这批删除记录已恢复或不属于当前文件，请重新查看处理结果",
            )
        for removal in removals:
            model = MODELS[removal.record_type]
            if await db.get(model, removal.record_id):
                raise MonthlyCloseConflict(
                    "restore_record_exists", "系统已存在同编号记录，不能覆盖恢复"
                )
            actions.append(
                {
                    "kind": "restore",
                    "removal_id": removal.removal_id,
                    "record_type": removal.record_type,
                    "service_type": removal.snapshot.get("service_type")
                    or (
                        "instay_cleaning"
                        if removal.record_type == "request"
                        else "cleaning"
                    ),
                    "record_id": removal.record_id,
                    "evidence_hash": digest(removal.snapshot),
                    "room_ref": removal.snapshot["room_id"],
                    "service_date": removal.snapshot.get("service_date")
                    or removal.snapshot.get("request_date")
                    or str(
                        removal.snapshot.get("completed_at")
                        or removal.snapshot.get("deadline")
                    )[:10],
                }
            )
    else:
        if spec.add_missing:
            chosen = [row for row in preview["actions"] if selected(row, spec)]
            # Per-row histories are inserted directly in the shared transaction;
            # the full live plan, including all source evidence, binds the selection.
            actions += [{"kind": "add", **row} for row in chosen]
        for item in raw.differences:
            row = item.model_dump()
            if not selected(row, spec):
                continue
            if (
                spec.delete_extra
                and item.table_count == 0
                and item.status in {"system_only", "duplicate"}
            ):
                if date.fromisoformat(item.service_date) >= today_cn():
                    raise MonthlyCloseConflict(
                        "active_cleaning_record",
                        "当天或未来的打扫记录需先在运营流程中核实，不能按历史表直接删除",
                    )
                for evidence in item.system_evidence:
                    kind = (
                        "history"
                        if evidence["kind"] == "history"
                        else "request"
                        if item.service_type == "instay_cleaning"
                        else "task"
                    )
                    record = await db.get(
                        MODELS[kind], evidence["record_id"], populate_existing=True
                    )
                    if record is None:
                        raise MonthlyCloseConflict(
                            "record_changed", "打扫记录已变化，请重新核对"
                        )
                    fees = []
                    if kind == "request" and record.expense_id:
                        fee = await db.get(Expense, record.expense_id)
                        if fee and not fee.is_deleted:
                            fees = [
                                {
                                    "expense_id": fee.expense_id,
                                    "amount": str(fee.amount),
                                }
                            ]
                    elif kind == "task":
                        fees = [
                            {"expense_id": fee.expense_id, "amount": str(fee.amount)}
                            for fee in await db.scalars(
                                select(Expense).where(
                                    Expense.category == ExpenseCategory.cleaning,
                                    Expense.room_id == record.room_id,
                                    Expense.order_id == record.order_id,
                                    Expense.expense_date
                                    == date.fromisoformat(item.service_date),
                                    Expense.is_deleted.is_(False),
                                )
                            )
                        ]
                    actions.append(
                        {
                            "kind": "delete",
                            "record_type": kind,
                            "record_id": evidence["record_id"],
                            "evidence_hash": digest(snapshot(record)),
                            "service_date": item.service_date,
                            "room_ref": item.room_ref,
                            "service_type": item.service_type,
                            "related_fees_unchanged": fees,
                        }
                    )
            if (
                item.status == "duplicate"
                and item.table_count
                and spec.duplicate != "ask"
            ):
                # A valid prior decision requires no second write.
                if any(
                    res["state"] == "applied"
                    and res["service_date"] == item.service_date
                    and res["room_ref"] == item.room_ref
                    and res["service_type"] == item.service_type
                    for res in preview["comparison"].get("resolutions", [])
                ):
                    continue
                decision = (
                    "count_once"
                    if spec.duplicate == "once" and item.table_count > 1
                    else "accept_table"
                )
                selection = {
                    "service_date": item.service_date,
                    "room_ref": item.room_ref,
                    "service_type": item.service_type,
                    "decision": decision,
                    "reason": reason,
                }
                result = await preview_work_resolution(
                    db, cycle, document, actor_id, selection
                )
                actions.append(
                    {
                        "kind": "resolve",
                        **selection,
                        "confirmed_count": result["confirmed_count"],
                        "resolution_hash": result["preview_hash"],
                    }
                )
    actions.sort(
        key=lambda action: (
            action["service_date"],
            action["room_ref"],
            action["kind"],
            action.get("record_id", ""),
        )
    )
    bound = {
        "version": VERSION,
        "actor": actor_id,
        "cycle": cycle.cycle_id,
        "document": document.document_id,
        "sha": document.sha256,
        "spec": spec.model_dump(),
        "reason": reason,
        "comparison": preview["comparison"],
        "actions": actions,
    }
    return {
        "preview_hash": digest(bound),
        "actions": actions,
        "comparison": preview["comparison"],
    }


async def execute_plan(db, cycle, document, actor_id, proposal, request_id):
    if proposal.spec and proposal.spec.repair_fees:
        from app.core.config import settings
        if not settings.MONTHLY_CLOSE_PROPOSAL_EXECUTION_ENABLED:
            raise MonthlyCloseConflict("fee_execution_disabled", "当前已暂停月结费用写入，方案尚未执行")
        from app.services.monthly_close.financial_lock import acquire_month_financial_lock
        from app.services.service_fee_ledger import lock_owner_service_fee_ledger
        await acquire_month_financial_lock(db, cycle.billing_month)
        # Same ordering as all participating financial writers: month -> cycle -> owner.
        cycle = await require_cycle_writable(db, cycle)
        for owner_id in sorted({a["owner_id"] for a in proposal.actions if a.get("kind") == "fee"}):
            await lock_owner_service_fee_ledger(db, owner_id)
    cycle = await require_cycle_writable(db, cycle)
    await _admin(db, actor_id, lock=True)
    await db.refresh(
        document,
        with_for_update=True,
        attribute_names=[
            "content",
            "filename",
            "sha256",
            "is_active",
            "source_type",
            "cycle_id",
        ],
    )
    receipt = await db.scalar(
        select(CleaningWorkImport).where(
            CleaningWorkImport.cycle_id == cycle.cycle_id,
            CleaningWorkImport.request_id == request_id,
        )
    )
    if receipt:
        if (
            receipt.created_by != actor_id
            or receipt.document_id != document.document_id
            or receipt.preview_hash != proposal.preview_hash
        ):
            raise MonthlyCloseConflict(
                "chat_request_conflict", "此确认请求已用于其他方案"
            )
        return receipt.result
    latest_run = await db.scalar(
        select(MonthlyCloseRun)
        .where(
            MonthlyCloseRun.cycle_id == cycle.cycle_id,
            MonthlyCloseRun.actor_id == actor_id,
            MonthlyCloseRun.tool_manifest_version == VERSION,
            MonthlyCloseRun.status == "succeeded",
            # A read/explanation does not silently revoke a displayed plan.
            # Revisions (including a no-op revision) and cancellation still do.
            (MonthlyCloseRun.output_payload_redacted["facts"]["state"].as_string() != "report")
            | (MonthlyCloseRun.output_payload_redacted["facts"]["reason"].as_string() != "")
            | (~MonthlyCloseRun.prompt_version.startswith("monthly-close-semantic-agent/")),
        )
        .order_by(MonthlyCloseRun.started_at.desc(), MonthlyCloseRun.run_id.desc())
        .limit(1)
    )
    if latest_run is not None and request_id != f"chat-{latest_run.run_id}":
        raise MonthlyCloseConflict(
            "chat_plan_superseded", "你已修改或取消了之前的方案，请确认最新一份方案"
        )
    fresh = await build_plan(
        db, cycle, document, actor_id, proposal.spec, proposal.reason
    )
    if fresh["preview_hash"] != proposal.preview_hash:
        raise MonthlyCloseConflict(
            "chat_plan_stale",
            "原表或系统记录已变化，旧方案没有执行。请让我重新生成方案",
        )
    if not fresh["actions"]:
        raise MonthlyCloseConflict("chat_empty_plan", "当前方案没有需要执行的修改")
    batch = CleaningWorkImport(
        import_id=f"CWI-{uuid4().hex[:20]}",
        cycle_id=cycle.cycle_id,
        document_id=document.document_id,
        request_id=request_id,
        preview_hash=proposal.preview_hash,
        created_by=actor_id,
        result={},
    )
    db.add(batch)
    await db.flush()
    removal_ids = []
    for index, action in enumerate(fresh["actions"]):
        kind = action["kind"]
        if kind == "fee":
            from app.services.monthly_close.work_record_fees import add_work_fee
            await add_work_fee(db, action, actor_id)
        elif kind == "delete":
            model = MODELS[action["record_type"]]
            pk = next(iter(model.__table__.primary_key.columns))
            record = await db.scalar(
                select(model)
                .where(pk == action["record_id"])
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if record is None or digest(snapshot(record)) != action["evidence_hash"]:
                raise MonthlyCloseConflict(
                    "chat_record_stale", "保存期间系统记录已变化，整批操作已停止"
                )
            removal = CleaningWorkRemoval(
                removal_id=f"CWD-{uuid4().hex[:20]}",
                import_id=batch.import_id,
                document_id=document.document_id,
                record_type=action["record_type"],
                record_id=action["record_id"],
                snapshot=snapshot(record),
                deleted_by=actor_id,
            )
            db.add(removal)
            await db.flush()
            await db.delete(record)
            removal_ids.append(removal.removal_id)
        elif kind == "add":
            db.add(
                CleaningWorkRecord(
                    record_id=f"CWR-{uuid4().hex[:20]}",
                    import_id=batch.import_id,
                    document_id=document.document_id,
                    document_sha256=document.sha256,
                    room_id=action["room_ref"],
                    service_date=date.fromisoformat(action["service_date"]),
                    service_type=action["service_type"],
                    source_sheet=action["source_sheet"],
                    source_row=action["source_row"],
                    created_by=actor_id,
                )
            )
        elif kind == "resolve":
            selection = {
                key: action[key]
                for key in (
                    "service_date",
                    "room_ref",
                    "service_type",
                    "decision",
                    "reason",
                )
            }
            await confirm_work_resolution(
                db,
                cycle,
                document,
                actor_id,
                selection,
                action["resolution_hash"],
                f"{request_id}-{index}",
                commit=False,
            )
        elif kind == "restore":
            removal = await db.scalar(
                select(CleaningWorkRemoval)
                .where(CleaningWorkRemoval.removal_id == action["removal_id"])
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if (
                removal is None
                or removal.restored_at
                or digest(removal.snapshot) != action["evidence_hash"]
            ):
                raise MonthlyCloseConflict(
                    "restore_changed", "这条删除记录已变化，请重新预览恢复"
                )
            db.add(reconstruct(MODELS[removal.record_type], removal.snapshot))
            removal.restored_at = datetime.now(timezone.utc)
            removal.restored_by = actor_id
        await db.flush()
    after = (await read_report(db, cycle, document, actor_id))[0]["comparison"]
    result = {
        "comparison": after,
        "actions": fresh["actions"],
        "removal_ids": removal_ids,
        "fee_summary": fresh.get("fee_summary"),
    }
    batch.result = result
    await log_action_tx(
        db,
        actor_id,
        "monthly_close.cleaning_chat.execute",
        "monthly_close_document",
        document.document_id,
        after_data={
            "import_id": batch.import_id,
            "actions": fresh["actions"],
            "reason": proposal.reason,
            "removal_ids": removal_ids,
        },
    )
    await db.commit()
    return result


def parse_spec(text, previous, report, billing_month):
    # The deterministic fallback must not turn a fee request into activity imports.
    from app.services.monthly_close.semantic_agent import fee_repair_request
    if fee_repair_request(text):
        return ChatSpec(repair_fees=True, service_type="instay_cleaning"), False
    if re.search(r"费用|金额|记账|补账", text) and re.search(r"补|改|删|执行", text):
        raise MonthlyCloseConflict("fee_scope_required", "请先单独核对续住保洁费用；可发送“核对并补齐续住保洁费用”查看具体方案")
    spec = ChatSpec.model_validate(
        previous.spec.model_dump() if previous and previous.spec else {}
    )
    compact = re.sub(r"\s+", "", text)
    # Questions and negation do not become deletion instructions.
    wants_delete = bool(re.search(r"删|移除|作废|撤销系统", compact)) and not re.search(
        r"(?:不|别|不要|先别).{0,4}(?:删|移除|作废)", compact
    )
    align = bool(
        re.search(
            r"(?:按|以|照).{0,14}(?:表|文件).{0,12}(?:准|走|对齐|同步|补齐|更新)|系统.{0,8}(?:改成|对齐).{0,8}表",
            compact,
        )
    )
    add = bool(re.search(r"补齐|补全|补上|补录|导入|漏记", compact))
    inspect = bool(
        re.search(r"查看|只看|看看|核对|查一下|为什么|怎么|哪些|分析|差异", compact)
    ) and not (wants_delete or align or add)
    if wants_delete:
        spec.delete_extra = True
    if align:
        spec.add_missing = True
        spec.delete_extra = True
    if add:
        spec.add_missing = True
    if re.search(r"(?:不|别|不要|先别).{0,4}(?:删|移除|作废)", compact):
        spec.delete_extra = False
    if re.search(r"只(?:删|移除)|仅(?:删|移除)", compact):
        spec.add_missing = False
        spec.duplicate = "ask"
    if re.search(
        r"重复.{0,12}(?:一次|1次|一条)|(?:只算|只留|保留)(?:一次|1次|一条)", compact
    ):
        spec.duplicate = "once"
    elif re.search(
        r"(?:确实|实际|真的).{0,8}(?:两次|二次|2次)|(?:重复|次数).{0,10}(?:按表|原表)|按表.{0,5}次数",
        compact,
    ):
        spec.duplicate = "table"
    if re.search(r"(?:所有|全部|整月|整个).{0,6}(?:房间|记录|处理|按表)?", compact):
        spec.rooms = []
        spec.service_date = None
        spec.service_type = None
    date_match = re.search(r"(20\d{2})[-/年.](\d{1,2})[-/月.](\d{1,2})日?", compact)
    without_date = compact
    if date_match:
        try:
            spec.service_date = date(*map(int, date_match.groups())).isoformat()
        except ValueError:
            raise MonthlyCloseConflict(
                "invalid_date", "日期不正确，请告诉我具体是哪一天"
            )
        without_date = without_date.replace(date_match.group(), "")
    short_date = re.search(r"(?<!\d)(\d{1,2})月(\d{1,2})[日号]", without_date)
    if short_date:
        try:
            spec.service_date = date(
                int(billing_month[:4]), *map(int, short_date.groups())
            ).isoformat()
        except ValueError:
            raise MonthlyCloseConflict(
                "invalid_date", "日期不正确，请告诉我具体是哪一天"
            )
    known = {row.room_ref for row in report.differences} | {
        row["room_ref"] for row in report.records
    }
    found_rooms = [
        room
        for room in known
        if re.search(
            r"(?<![A-Za-z0-9])" + re.escape(room) + r"(?![A-Za-z0-9])", without_date
        )
    ]
    if found_rooms:
        spec.rooms = sorted(found_rooms)
    elif re.search(r"(?<!\d)\d{3,4}(?!\d)", without_date):
        raise MonthlyCloseConflict(
            "unknown_room_selector",
            "没有唯一找到你指定的房间，请使用差异清单里的完整房号",
        )
    if "续住" in compact:
        spec.service_type = "instay_cleaning"
    elif "正常打扫" in compact or "退房打扫" in compact:
        spec.service_type = "cleaning"
    return spec, inspect


async def answer_work_chat(
    db, cycle, actor_id, text, document_ids, previous, previous_run_id,
    *, selection=None, inspect_only=False, restore_only=False, explain_only=False, cancel_only=False,
):
    await _admin(db, actor_id)
    base = {"billing_month": cycle.billing_month, "request_text": text[:4000]}
    ids = list(dict.fromkeys(document_ids))
    if not ids and previous and previous.document_id:
        ids = [previous.document_id]
    query = (
        select(MonthlyCloseDocument)
        .where(
            MonthlyCloseDocument.cycle_id == cycle.cycle_id,
            MonthlyCloseDocument.source_type == "cleaning_statement",
            MonthlyCloseDocument.is_active.is_(True),
        )
        .options(undefer(MonthlyCloseDocument.content))
    )
    if ids:
        query = query.where(MonthlyCloseDocument.document_id.in_(ids))
    documents = list(await db.scalars(query))
    if len(documents) != 1:
        return WorkChatFacts(
            **base,
            state="clarification",
            message="请在聊天里选择要核对的那份保洁表，我会在这里列出结果和处理方案。",
        )
    document = documents[0]
    base.update(document_id=document.document_id, filename=document.filename)
    compact = re.sub(r"[\s，。！!]+", "", text)
    if cancel_only or compact in {"取消", "取消方案", "先不执行", "先别执行", "不要执行"}:
        comparison = (await read_report(db, cycle, document, actor_id))[0]["comparison"] if cancel_only else None
        return WorkChatFacts(
            **base,
            comparison=comparison,
            state="cancelled",
            message="已取消尚未执行的方案。你可以继续告诉我新的处理要求。",
            spec=ChatSpec(),
        )
    if compact in {
        "确认执行",
        "确认删除",
        "确认补齐",
        "确认处理",
        "确认恢复",
        "执行这个方案",
        "按这个方案执行",
        "确认",
        "通过",
    }:
        if (
            not previous
            or previous.document_id != document.document_id
            or previous.state not in {"proposal", "completed"}
            or not previous.preview_hash
            or not previous_run_id
        ):
            return WorkChatFacts(
                **base,
                state="clarification",
                message="我还没有一份可确认的具体方案。请先告诉我要怎么处理，例如“按表补齐，并删除系统多余记录”。",
            )
        if previous.state == "completed":
            return previous.model_copy(
                update={"message": "这份方案已经执行过了，没有重复修改。"}
            )
        try:
            result = await execute_plan(
                db, cycle, document, actor_id, previous, f"chat-{previous_run_id}"
            )
        except Exception as exc:
            # The API transaction must roll back all staged record mutations.
            await db.rollback()
            if isinstance(exc, MonthlyCloseConflict):
                return WorkChatFacts(
                    **base,
                    state="conflict",
                    message=exc.message if hasattr(exc, "message") else str(exc),
                    spec=previous.spec,
                )
            from sqlalchemy.exc import IntegrityError

            if isinstance(exc, IntegrityError):
                return WorkChatFacts(
                    **base,
                    state="conflict",
                    message="系统已有冲突记录，整批操作未保存。请重新查看记录后生成方案。",
                    spec=previous.spec,
                )
            raise
        counts = {
            kind: sum(action["kind"] == kind for action in result["actions"])
            for kind in ("delete", "add", "resolve", "restore")
        }
        remaining = len(result["comparison"]["differences"])
        message = (
            f"已完成：删除 {counts['delete']} 条系统打扫记录，补齐 {counts['add']} 条，核实 {counts['resolve']} 项，恢复 {counts['restore']} 条。重新核对后剩余 {remaining} 项。"
            if remaining
            else f"方案已执行，这份保洁表已核对完成。删除 {counts['delete']} 条、补齐 {counts['add']} 条、核实 {counts['resolve']} 项、恢复 {counts['restore']} 条。"
        )
        if previous.spec and previous.spec.repair_fees:
            fees = result["fee_summary"]
            message = f"已补记续住保洁费用 {fees['amount']} 元，共 {len(fees['actions'])} 笔；仍有 {len(fees['unresolved'])} 项费用需要核实。已有费用没有重复记账。退房保洁、洗涤、日耗及其他支出需分别核对。"
            if fees.get("pending_settlement_ids"):
                message += "本月已有待确认业主结算单，费用变化后须重新生成并复核，不能确认旧金额。"
        return WorkChatFacts(
            **base,
            state="completed",
            message=message,
            comparison=result["comparison"],
            actions=result["actions"],
            removal_ids=result["removal_ids"],
            spec=previous.spec,
            preview_hash=previous.preview_hash,
            reason=previous.reason,
            fee_summary=result.get("fee_summary"),
        )
    try:
        preview, raw = await read_report(db, cycle, document, actor_id)
        if selection is not None:
            spec = ChatSpec.model_validate(selection)
            known = {row.room_ref for row in raw.differences} | {row["room_ref"] for row in raw.records}
            if spec.restore_ids or any(room not in known for room in spec.rooms):
                raise MonthlyCloseConflict("unknown_room_selector", "没有找到你指定的房间，请使用这份表中的完整房号")
            if spec.service_date:
                try:
                    selected_month = date.fromisoformat(spec.service_date).strftime("%Y-%m")
                except ValueError as exc:
                    raise MonthlyCloseConflict("invalid_date", "日期不正确，请说明要处理哪一天") from exc
                if selected_month != cycle.billing_month:
                    raise MonthlyCloseConflict("different_month", "指定日期不属于当前月份，请先切换到对应月份")
            inspect = False
        elif inspect_only:
            spec, inspect = ChatSpec(), True
        elif restore_only or re.search(r"撤回|恢复|撤销刚才|撤销删除", text):
            removal_ids = previous.removal_ids if previous else []
            if not removal_ids:
                return WorkChatFacts(
                    **base,
                    state="clarification",
                    message="请在对应的删除结果下选择恢复，我需要知道要恢复哪一批记录。",
                )
            spec, inspect = ChatSpec(restore_ids=removal_ids), False
        else:
            spec, inspect = parse_spec(text, previous, raw, cycle.billing_month)
        reason = text.strip()[:1000]
        if inspect or not (
            spec.add_missing
            or spec.repair_fees
            or spec.delete_extra
            or spec.duplicate != "ask"
            or spec.restore_ids
        ):
            comparison = preview["comparison"]
            report = WorkLogComparison.model_validate(comparison)
            explanation = ""
            if explain_only and previous and previous.actions:
                explanation = "方案按日期、房间和打扫类型逐项对照原表：缺少的记录可补齐，表外记录可删除，重复次数按你核实的结果处理。只有确认方案后才修改；关联费用不会随打扫记录自动删除。\n"
                if previous.state == "proposal":
                    return previous.model_copy(update={"request_text":text[:4000], "message":explanation + "下面保留原方案供你核实；如果记录已经变化，确认时会要求重新生成。"})
            return WorkChatFacts(
                **base,
                state="report",
                message=explanation + f"已读出 {report.record_count} 条打扫记录，已对应 {report.matched_count} 条，还有 {len(report.differences)} 项差异。你可以继续询问依据或告诉我处理要求。",
                comparison=report,
                spec=spec,
            )
        if previous and previous.state == "proposal" and not (spec.restore_ids):
            reason = (previous.reason + "；" + reason)[-1000:]
        plan = await build_plan(db, cycle, document, actor_id, spec, reason)
        if not plan["actions"]:
            return WorkChatFacts(
                **base,
                state="report",
                message=(f"本次没有可直接补记的续住保洁费用，仍有 {len(plan['fee_summary']['unresolved'])} 项需要核实；打扫次数核对完成不代表费用已核齐。" if spec.repair_fees else "按你指定的范围，没有需要执行的修改。剩余差异在下面；可以继续告诉我具体房间或重复项的实际次数。"),
                comparison=plan["comparison"],
                spec=spec,
                reason=reason,
                fee_summary=plan.get("fee_summary"),
            )
        duplicate_count = sum(
            row.status == "duplicate"
            and row.table_count > 1
            and selected(row.model_dump(), spec)
            for row in raw.differences
        )
        message = f"我拟好了 {len(plan['actions'])} 项修改，具体清单在下面。确认后会实际更新系统打扫记录；删除记录会保留恢复快照。"
        if duplicate_count and spec.duplicate == "ask":
            message += "重复项暂未改动，请告诉我是重复填写一次，还是确实打扫多次。"
        message += "你可以继续修改方案，或回复“确认执行”。"
        if spec.repair_fees:
            fees = plan["fee_summary"]
            message = f"按当前收费标准，可补记续住保洁费用 {fees['amount']} 元，共 {len(fees['actions'])} 笔；另有 {len(fees['unresolved'])} 项待核实。下方列出日期、房间、单价、已有金额和本次补记金额。确认后写入财务账本。"
            if fees.get("pending_settlement_ids"):
                message += "本月已有待确认业主结算单；补账后须重新生成并复核，原快照不会自动改写或确认。"
        return WorkChatFacts(
            **base,
            state="proposal",
            message=message,
            comparison=plan["comparison"],
            spec=spec,
            actions=plan["actions"],
            preview_hash=plan["preview_hash"],
            reason=reason,
            fee_summary=plan.get("fee_summary"),
        )
    except MonthlyCloseConflict as exc:
        return WorkChatFacts(**base, state="conflict", message=str(exc))
