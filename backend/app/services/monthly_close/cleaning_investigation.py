"""Read-only cleaning investigation, with server-owned evidence and follow-up state.

No workbook prose or employee prose is persisted or sent to a model. A small
business vocabulary selects the investigation; the existing matching service
decides matches and Decimal computes differences. Unknown explanations remain
unknown, and an employee statement never resolves a financial issue.
"""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
import json
import re
from typing import Literal
import unicodedata

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expense import Expense, ExpenseCategory
from app.models.monthly_close import (
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseServiceLine,
)
from app.models.order import Order
from app.models.order_room import OrderRoom
from app.models.room import Room
from app.models.task import Task, TaskType
from app.services.monthly_close.projection import FINANCIAL_DETAIL_ROLES
from app.services.monthly_close.service_reconciliation import (
    _match_expense,
    _reference_key,
    _resolve_room_reference,
    _room_alias_index,
)

MAX_LINES = 2000
PAGE_SIZE = 10
CLEANING_TOOL_VERSION = "monthly-close-cleaning-investigation/v1"


class DTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


class CleaningRequest(DTO):
    action: Literal[
        "list",
        "select",
        "continue",
        "next_page",
        "previous_page",
        "explain",
        "note",
        "withdraw_note",
        "draft",
    ] = "list"
    ordinal: int | None = Field(default=None, ge=1, le=MAX_LINES)
    amount: str | None = None
    room: str | None = None
    note_code: (
        Literal["extra_cleaning", "replacement_statement", "service_not_performed"]
        | None
    ) = None


class SourceEvidence(DTO):
    document_id: str
    line_id: str
    row_number: int
    amount: str
    document_hash: str


class ExpenseEvidence(DTO):
    expense_id: str
    amount: str
    expense_date: str
    order_id: str | None = None


class OrderEvidence(DTO):
    order_id: str
    room_id: str | None
    check_in: str
    check_out: str
    status: str
    stay_group_id: str | None = None
    relationship: Literal[
        "expense", "statement_reference", "room_date_candidate", "stay_group"
    ] = "expense"


class TaskEvidence(DTO):
    task_id: str
    status: str
    review_status: str | None
    completed_at: str | None


class CleaningCase(DTO):
    case_id: str
    ordinal: int
    document_id: str
    service_date: str | None
    room_id: str | None
    service_type: str
    status: str
    vendor_amount: str
    system_amount: str | None
    difference: str | None
    summary: str
    question: str
    sources: list[SourceEvidence]
    related_sources: list[SourceEvidence] = Field(default_factory=list)
    expenses: list[ExpenseEvidence]
    orders: list[OrderEvidence] = Field(default_factory=list)
    tasks: list[TaskEvidence] = Field(default_factory=list)
    evidence_hash: str = ""
    detail_limited: bool = False


class CleaningNote(DTO):
    case_id: str
    evidence_hash: str
    note_code: Literal[
        "extra_cleaning", "replacement_statement", "service_not_performed"
    ]


class InvestigationFinding(DTO):
    id: str
    text: str
    evidence_refs: list[str]


class CorrectionChange(DTO):
    order_id: str
    room_id: str
    category: str
    before_amount: str | None
    before_date: str | None
    after_date: str
    before_payer: str | None
    after_payer: str
    after_amount: str


class CorrectionDraft(DTO):
    state: Literal["ready", "needs_evidence", "no_change", "limited"]
    message: str
    changes: list[CorrectionChange] = Field(default_factory=list)
    amount_impact: str = "0.00"
    evidence_hash: str = ""
    case_related: bool = False


class CleaningInvestigationFacts(DTO):
    kind: Literal["cleaning_investigation"] = "cleaning_investigation"
    billing_month: str
    state: Literal[
        "ready", "needs_selection", "no_source", "unavailable", "too_large", "forbidden"
    ]
    findings: list[InvestigationFinding] = Field(default_factory=list, max_length=16)
    investigation_steps: list[str] = Field(default_factory=list, max_length=5)
    reasoning_mode: Literal["not_needed", "model", "disabled", "fallback"] = (
        "not_needed"
    )
    correction_draft: CorrectionDraft | None = None
    checked_group_count: int = 0
    difference_count: int = 0
    page: int = 0
    has_more: bool = False
    cases: list[CleaningCase] = Field(default_factory=list)
    selected: CleaningCase | None = None
    # Holds the last visible list, including when one case is selected. Ordinal
    # follow-ups must never silently refer to a newly sorted/current list.
    case_order: list[str] = Field(default_factory=list)
    evidence_changed: bool = False
    note_code: (
        Literal["extra_cleaning", "replacement_statement", "service_not_performed"]
        | None
    ) = None
    note_case_id: str | None = None
    saved_notes: list[CleaningNote] = Field(default_factory=list, max_length=MAX_LINES)
    message: str
    checked_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


def parse_cleaning_request(
    text: str, *, has_context: bool = False
) -> CleaningRequest | None:
    """Extract only bounded business selectors; callers reject secrets first."""
    value = unicodedata.normalize("NFKC", text).strip().lower()
    cleaning = any(word in value for word in ("保洁", "打扫", "清洁", "阿姨"))
    if not cleaning and not has_context:
        return None
    ordinal_matches = re.findall(
        r"第\s*([0-9]{1,4}|[一二三四五六七八九十]+)\s*[笔条项个]", value
    )
    if len(ordinal_matches) > 1:
        return CleaningRequest(action="list")
    number = None
    if ordinal_matches:
        raw = ordinal_matches[0]
        numbers = {
            "一": 1,
            "二": 2,
            "三": 3,
            "四": 4,
            "五": 5,
            "六": 6,
            "七": 7,
            "八": 8,
            "九": 9,
            "十": 10,
        }
        number = int(raw) if raw.isdigit() else numbers.get(raw)
        if not number or not 1 <= number <= MAX_LINES:
            return CleaningRequest(action="list")
    amount = re.search(
        r"差(?:了|额)?\s*(\d{1,7}(?:\.\d{1,2})?)(?![\d.])(?:\s*元)?", value
    )
    room = re.search(
        r"(?:房间|房号)\s*([a-z]?\d{1,6})|([a-z]?\d{1,6})\s*(?:号房|房间)", value
    )
    selectors = {
        "ordinal": number,
        "amount": amount[1] if amount else None,
        "room": (room[1] or room[2]) if room else None,
    }
    if has_context and any(
        phrase in value
        for phrase in (
            "撤回说明",
            "撤回这条说明",
            "撤回刚才的说明",
        )
    ):
        return CleaningRequest(action="withdraw_note", **selectors)
    if has_context and any(
        phrase in value for phrase in ("不是额外保洁", "没有额外保洁")
    ):
        uncertain = any(
            word in value
            for word in ("是不是", "会不会", "不确定", "可能", "是否", "吗", "？", "?")
        )
        return CleaningRequest(
            action="explain" if uncertain else "withdraw_note", **selectors
        )
    if any(
        word in value
        for word in ("删除", "作废", "批准", "执行", "改成", "关账", "导入", "提交方案")
    ):
        return None
    if any(word in value for word in ("方案", "草稿")):
        return CleaningRequest(action="draft", **selectors)
    # A different subject explicitly leaves this investigation.
    if not cleaning and any(
        word in value for word in ("ota", "布草", "水电", "业主", "运营支出")
    ):
        return None
    if has_context and any(
        word in value for word in ("额外", "加做", "又打扫", "加了一次")
    ):
        if any(
            word in value
            for word in (
                "吗",
                "会不会",
                "不是",
                "没有",
                "没做",
                "不确定",
                "可能",
                "是否",
                "是不是",
                "？",
                "?",
            )
        ):
            return CleaningRequest(action="explain", **selectors)
        return CleaningRequest(action="note", note_code="extra_cleaning", **selectors)
    if (
        has_context
        and any(word in value for word in ("修订版", "替换旧", "完整新版"))
        and not any(
            word in value
            for word in (
                "吗",
                "会不会",
                "不是",
                "不确定",
                "可能",
                "是否",
                "是不是",
                "？",
                "?",
            )
        )
    ):
        return CleaningRequest(
            action="note", note_code="replacement_statement", **selectors
        )
    if (
        has_context
        and any(word in value for word in ("没打扫", "没有打扫", "未打扫"))
        and not any(
            word in value
            for word in ("吗", "是否", "是不是", "不确定", "可能", "？", "?")
        )
    ):
        return CleaningRequest(
            action="note", note_code="service_not_performed", **selectors
        )
    if "下一页" in value:
        return CleaningRequest(action="next_page")
    if "上一页" in value:
        return CleaningRequest(action="previous_page")
    if number:
        return CleaningRequest(action="select", ordinal=number)
    if amount or room:
        return CleaningRequest(
            action="select",
            amount=amount[1] if amount else None,
            room=(room[1] or room[2]) if room else None,
        )
    if any(word in value for word in ("继续", "重新查", "再查", "复查", "接着")):
        return CleaningRequest(action="continue")
    if has_context and any(
        word in value
        for word in (
            "修订版",
            "替换旧",
            "完整新版",
            "为什么",
            "原因",
            "依据",
            "这笔",
            "这一笔",
            "怎么处理",
            "怎么回事",
            "多算",
            "少算",
            "凭证",
            "续住",
            "换房",
            "任务",
            "单价",
            "收费",
            "重复",
            "续住",
            "换房",
        )
    ):
        return CleaningRequest(action="explain", **selectors)
    if cleaning and any(
        word in value for word in ("差异", "不一致", "对不上", "查", "核对")
    ):
        return CleaningRequest(action="list")
    return None


def _money(value) -> str:
    return f"{Decimal(str(value)).quantize(Decimal('0.01')):.2f}"


def _hash(value) -> str:
    return sha256(
        json.dumps(value, sort_keys=True, ensure_ascii=False, default=str).encode()
    ).hexdigest()


def _explanation(status: str, vendor: str, system: str | None) -> tuple[str, str]:
    if status == "matched":
        return (
            "当前费用与供应商金额一致。金额一致不代表服务发生事实已核实。",
            "继续查看凭证或回到月结核对结果。",
        )
    if status == "amount_mismatch":
        return (
            f"供应商合计 ¥{vendor}，当前系统费用 ¥{system}；金额不一致，尚不能判断哪一方应调整。",
            "是否有额外保洁、单价调整或修订账单？请补充对应记录。",
        )
    if status == "duplicate":
        return (
            "多份有效账单出现相同业务明细，可能是重复上传或修订版本；不能据此认定重复收费。",
            "这些文件是替换旧账单，还是分别发生的服务？",
        )
    if status == "ambiguous":
        return (
            "同一条供应商明细对应多个房间或多笔系统费用，无法唯一匹配。",
            "请确认实际房间，以及是否分次安排了保洁。",
        )
    return (
        "按当前房间、订单或服务日期未找到唯一可匹配的系统服务费；这不证明服务没有发生。",
        "请核实保洁是否实际发生，以及账单房号、日期和订单是否正确。",
    )


async def _case_details(
    db: AsyncSession, case: CleaningCase, order_ref: str | None
) -> CleaningCase:
    """Load operational fields only; do not serialize ORM objects or notes."""
    order_ids = {item.order_id for item in case.expenses if item.order_id}
    relationship = "expense"
    if not order_ids and order_ref:
        # A reference match is evidence of an association, never proof that a
        # charge is correct. Do not expose the external free-text reference.
        order_ids = set(
            await db.scalars(
                select(Order.order_id)
                .where(
                    or_(
                        Order.order_id == order_ref.strip(),
                        Order.platform_order_id == order_ref.strip(),
                    ),
                    Order.is_deleted.is_(False),
                )
                .limit(101)
            )
        )
        relationship = "statement_reference"
    if not order_ids and case.room_id and case.service_date:
        service_date = date.fromisoformat(case.service_date)
        order_ids = set(
            await db.scalars(
                select(OrderRoom.order_id)
                .join(Order, Order.order_id == OrderRoom.order_id)
                .where(
                    OrderRoom.room_id == case.room_id,
                    OrderRoom.check_in_date <= service_date,
                    OrderRoom.check_out_date >= service_date,
                    Order.is_deleted.is_(False),
                )
                .distinct()
                .limit(101)
            )
        )
        # Legacy single-room orders without room rows remain valid evidence.
        legacy_ids = await db.scalars(
            select(Order.order_id)
            .where(
                Order.room_id == case.room_id,
                Order.check_in_date <= service_date,
                Order.check_out_date >= service_date,
                Order.is_deleted.is_(False),
                ~select(OrderRoom.order_room_id)
                .where(OrderRoom.order_id == Order.order_id)
                .exists(),
            )
            .limit(101)
        )
        order_ids.update(legacy_ids)
        relationship = "room_date_candidate"
    order_rows = (
        list(
            await db.scalars(
                select(Order)
                .execution_options(populate_existing=True)
                .where(Order.order_id.in_(order_ids), Order.is_deleted.is_(False))
                .order_by(Order.order_id)
            )
        )
        if order_ids
        else []
    )
    groups = {item.stay_group_id for item in order_rows if item.stay_group_id}
    if groups:
        order_rows = list(
            await db.scalars(
                select(Order)
                .execution_options(populate_existing=True)
                .where(Order.stay_group_id.in_(groups), Order.is_deleted.is_(False))
                .order_by(Order.order_id)
                .limit(101)
            )
        ) + [item for item in order_rows if not item.stay_group_id]
    detail_limited = len(order_rows) > 100
    order_rows = list({item.order_id: item for item in order_rows}.values())[:100]
    ids = [item.order_id for item in order_rows]
    rooms = (
        list(
            await db.scalars(
                select(OrderRoom)
                .execution_options(populate_existing=True)
                .where(OrderRoom.order_id.in_(ids))
                .order_by(
                    OrderRoom.order_id, OrderRoom.position, OrderRoom.order_room_id
                )
                .limit(201)
            )
        )
        if ids
        else []
    )
    by_order = defaultdict(list)
    for room in rooms:
        by_order[room.order_id].append(room)
    orders = []
    for order in order_rows:
        for room in by_order[order.order_id] or [order]:
            orders.append(
                OrderEvidence(
                    order_id=order.order_id,
                    room_id=room.room_id,
                    check_in=room.check_in_date.isoformat(),
                    check_out=room.check_out_date.isoformat(),
                    status=order.order_status.value,
                    stay_group_id=order.stay_group_id,
                    relationship=relationship
                    if order.order_id in order_ids
                    else "stay_group",
                )
            )
    tasks = (
        list(
            await db.scalars(
                select(Task)
                .execution_options(populate_existing=True)
                .where(
                    Task.order_id.in_(ids),
                    Task.task_type == TaskType.cleaning,
                    Task.room_id == case.room_id,
                )
                .order_by(Task.task_id)
                .limit(101)
            )
        )
        if ids and case.room_id
        else []
    )
    details = case.model_copy(
        update={
            "orders": orders[:200],
            "tasks": [
                TaskEvidence(
                    task_id=item.task_id,
                    status=item.status.value,
                    review_status=item.review_status
                    if item.review_status in {"pending_review", "approved", "rejected"}
                    else None,
                    completed_at=item.completed_at.isoformat()
                    if item.completed_at
                    else None,
                )
                for item in tasks[:100]
            ],
            "detail_limited": detail_limited or len(rooms) > 200 or len(tasks) > 100,
        }
    )
    details.evidence_hash = _hash(
        details.model_dump(exclude={"evidence_hash", "ordinal"})
    )
    return details


async def investigate_cleaning(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    role: str,
    request: CleaningRequest,
    previous: CleaningInvestigationFacts | None = None,
    ready_document_ids: set[str] | None = None,
) -> CleaningInvestigationFacts:
    result = CleaningInvestigationFacts(
        billing_month=cycle.billing_month, state="ready", message=""
    )
    if role not in FINANCIAL_DETAIL_ROLES:
        return result.model_copy(
            update={
                "state": "forbidden",
                "message": "保洁金额与关联费用依据仅向管理员和财务展示。你可以继续提交本人负责的资料，请财务人员调查具体差异。",
            }
        )
    documents = list(
        await db.scalars(
            select(MonthlyCloseDocument)
            .execution_options(populate_existing=True)
            .where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.source_type == "cleaning_statement",
                MonthlyCloseDocument.is_active.is_(True),
            )
            .order_by(MonthlyCloseDocument.document_id)
        )
    )
    docs = {item.document_id: item for item in documents}
    if not docs:
        return result.model_copy(
            update={
                "state": "no_source",
                "message": "本月还没有有效的保洁账单。请先上传并确认识别结果，再查保洁差异。",
            }
        )
    if any(
        item.processing_status != "processed"
        or (
            ready_document_ids is not None
            and item.document_id not in ready_document_ids
        )
        for item in documents
    ):
        return result.model_copy(
            update={
                "state": "unavailable",
                "message": "保洁账单尚未全部完成识别，请先处理文件卡中的待确认或识别失败事项，再调查差异。",
            }
        )
    lines = list(
        await db.scalars(
            select(MonthlyCloseServiceLine)
            .where(MonthlyCloseServiceLine.document_id.in_(docs))
            .order_by(
                MonthlyCloseServiceLine.document_id,
                MonthlyCloseServiceLine.source_sheet,
                MonthlyCloseServiceLine.source_row_number,
            )
            .limit(MAX_LINES + 1)
        )
    )
    if len(lines) > MAX_LINES:
        return result.model_copy(
            update={
                "state": "too_large",
                "message": "本月保洁明细超过本次调查的 2000 行上限，请先使用文件详情逐份核对；助理没有把部分结果当作全月结论。",
            }
        )
    aliases = _room_alias_index(
        list((await db.execute(select(Room.room_id, Room.room_name))).tuples())
    )
    grouped = defaultdict(list)
    for line in lines:
        grouped[(line.document_id, line.business_key or line.line_id)].append(line)
    resolved = {
        key: _resolve_room_reference(group[0].room_ref or "", aliases)[0]
        for key, group in grouped.items()
    }
    room_ids = {room for room in resolved.values() if room}
    expense_rows = (
        list(
            (
                await db.execute(
                    select(Expense, Order.platform_order_id)
                    .execution_options(populate_existing=True)
                    .outerjoin(Order, Order.order_id == Expense.order_id)
                    .where(
                        Expense.is_deleted.is_(False),
                        Expense.is_service_fee.is_(True),
                        Expense.category == ExpenseCategory.cleaning,
                        Expense.room_id.in_(room_ids),
                    )
                    .order_by(Expense.expense_id)
                    .limit(10001)
                )
            ).tuples()
        )
        if room_ids
        else []
    )
    if len(expense_rows) > 10000:
        return result.model_copy(
            update={
                "state": "too_large",
                "message": "关联费用记录过多，本次调查未给出部分匹配结论，请先在财务明细中缩小核对范围。",
            }
        )
    by_order, by_date = defaultdict(list), defaultdict(list)
    expenses_by_id = {expense.expense_id: expense for expense, _ in expense_rows}
    known_room_ids = {room for candidates in aliases.values() for room in candidates}
    for expense, platform_id in expense_rows:
        if expense.order_id:
            for reference in {
                _reference_key(expense.order_id),
                _reference_key(platform_id),
            } - {""}:
                by_order[(expense.category, expense.room_id, reference)].append(expense)
        by_date[(expense.category, expense.room_id, expense.expense_date)].append(
            expense
        )
    duplicate_keys = defaultdict(set)
    for line in lines:
        if line.business_key:
            duplicate_keys[line.business_key].add(line.document_id)
    cases = []
    case_order_refs = {}
    for key, group in grouped.items():
        first, room_id = group[0], resolved[key]
        amount = sum((item.amount for item in group), Decimal("0"))
        vendor = _money(amount)
        status, expense_id, _, detail = _match_expense(
            service_type=first.service_type,
            order_ref=first.order_ref,
            room_ref=room_id or "",
            service_date=first.service_date,
            amount=amount,
            by_order=by_order,
            by_date=by_date,
        )
        if not room_id:
            status = "ambiguous"
        if len(duplicate_keys[first.business_key]) > 1:
            status = "duplicate"
        candidate_ids = set(detail.get("candidate_expense_ids", [])) | (
            {expense_id} if expense_id else set()
        )
        expenses = [
            ExpenseEvidence(
                expense_id=item.expense_id,
                amount=_money(item.amount),
                expense_date=item.expense_date.isoformat(),
                order_id=item.order_id,
            )
            for candidate_id in sorted(candidate_ids)
            if (item := expenses_by_id.get(candidate_id)) is not None
        ]
        system = (
            detail.get("system_amount")
            if status in {"matched", "amount_mismatch"}
            else None
        )
        summary, question = _explanation(status, vendor, system)
        case = CleaningCase(
            case_id="MCCASE-" + _hash(key)[:16],
            ordinal=0,
            document_id=first.document_id,
            service_date=first.service_date.isoformat() if first.service_date else None,
            room_id=room_id if room_id in known_room_ids else None,
            service_type=first.service_type
            if first.service_type in {"cleaning", "instay_cleaning"}
            else "other_service",
            status=status,
            vendor_amount=vendor,
            system_amount=system,
            difference=_money(amount - Decimal(system)) if system is not None else None,
            summary=summary,
            question=question,
            sources=[
                SourceEvidence(
                    document_id=item.document_id,
                    line_id=item.line_id,
                    row_number=item.source_row_number,
                    amount=_money(item.amount),
                    document_hash=docs[item.document_id].sha256,
                )
                for item in group
            ],
            expenses=expenses,
        )
        case.evidence_hash = _hash(
            [
                case.model_dump(exclude={"evidence_hash", "ordinal"}),
                docs[first.document_id].sha256,
            ]
        )
        if status == "duplicate":
            case.related_sources = [
                SourceEvidence(
                    document_id=item.document_id,
                    line_id=item.line_id,
                    row_number=item.source_row_number,
                    amount=_money(item.amount),
                    document_hash=docs[item.document_id].sha256,
                )
                for item in lines
                if item.business_key == first.business_key
                and item.document_id != first.document_id
            ]
        cases.append(case)
        case_order_refs[case.case_id] = first.order_ref
    differences = [case for case in cases if case.status != "matched"]
    result.checked_group_count, result.difference_count = len(cases), len(differences)
    by_id = {case.case_id: case for case in cases}
    notes = (
        {note.case_id: note for note in previous.saved_notes if note.case_id in by_id}
        if previous
        else {}
    )
    result.saved_notes = list(notes.values())
    selected_id = None
    if request.ordinal is not None:
        order = previous.case_order if previous else []
        selected_id = (
            order[request.ordinal - 1] if request.ordinal <= len(order) else None
        )
        if selected_id is None or selected_id not in by_id:
            result.state, result.message = (
                "unavailable",
                "上一轮的这笔明细已不可用，或序号不在清单中。请重新查保洁差异，再选择具体一笔。",
            )
            return result
    elif (
        request.action in {"explain", "note", "continue", "withdraw_note", "draft"}
        and not request.amount
        and not request.room
        and previous
        and previous.selected
    ):
        selected_id = previous.selected.case_id
        if selected_id not in by_id:
            result.state, result.message = (
                "unavailable",
                "这笔明细对应的资料已撤销或重新解析，请重新查保洁差异；原来的说明不会自动套用到其他明细。",
            )
            return result
    elif request.amount or request.room:
        filtered = differences
        if request.amount:
            filtered = [
                case
                for case in filtered
                if case.difference is not None
                and abs(Decimal(case.difference)) == Decimal(request.amount)
            ]
        if request.room:
            room_id = _resolve_room_reference(request.room, aliases)[0]
            filtered = [
                case for case in filtered if case.room_id and case.room_id == room_id
            ]
        if len(filtered) == 1:
            selected_id = filtered[0].case_id
        else:
            differences = filtered
            result.state = "needs_selection"
    if selected_id:
        selected = await _case_details(
            db, by_id[selected_id], case_order_refs[selected_id]
        )
        order = (
            previous.case_order
            if previous and selected_id in previous.case_order
            else [selected_id]
        )
        selected.ordinal = order.index(selected_id) + 1
        result.case_order, result.selected = order, selected
        result.evidence_changed = bool(
            previous
            and previous.selected
            and previous.selected.case_id == selected_id
            and previous.selected.evidence_hash != selected.evidence_hash
        )
        prior_note = notes.get(selected_id)
        if prior_note and prior_note.evidence_hash != selected.evidence_hash:
            result.evidence_changed = True
            notes.pop(selected_id)
        if not result.evidence_changed:
            if request.action == "withdraw_note":
                notes.pop(selected_id, None)
            elif request.note_code:
                result.note_code, result.note_case_id = request.note_code, selected_id
                notes[selected_id] = CleaningNote(
                    case_id=selected_id,
                    evidence_hash=selected.evidence_hash,
                    note_code=request.note_code,
                )
            elif prior_note:
                result.note_code, result.note_case_id = (
                    prior_note.note_code,
                    selected_id,
                )
            elif previous and previous.note_case_id == selected_id:
                result.note_code, result.note_case_id = previous.note_code, selected_id
        result.saved_notes = list(notes.values())
        result.message = (
            "相关记录发生了变化，已重新查询；此前的说明需要重新核实。"
            if result.evidence_changed
            else "已查询这笔账单的当前费用、关联订单和保洁任务。"
        ) + (
            "你的说明已作为待核实线索保存，尚未作为凭证，也没有改动费用。"
            if result.note_code
            else ""
        )
        if request.action == "withdraw_note":
            result.message += "已撤回这笔先前的业务说明，核对差异和财务记录未改变。"
        return result
    result.case_order = [case.case_id for case in differences]
    page = (
        previous.page
        if previous and request.action in {"next_page", "previous_page"}
        else 0
    )
    if request.action == "next_page":
        page += 1
    if request.action == "previous_page":
        page -= 1
    result.page = max(0, min(page, max(0, (len(differences) - 1) // PAGE_SIZE)))
    start = result.page * PAGE_SIZE
    for ordinal, case in enumerate(differences, 1):
        case.ordinal = ordinal
    result.cases = differences[start : start + PAGE_SIZE]
    result.has_more = start + PAGE_SIZE < len(differences)
    result.message = f"按当前系统服务费重新核对了 {len(cases)} 组保洁明细，发现 {result.difference_count} 组需要核实。请选择一笔查看依据。"
    if request.amount or request.room:
        result.message += f"其中 {len(differences)} 组符合本次筛选。"
    if not cases:
        result.message = "保洁账单中尚无可调查明细，请先检查文件识别结果。"
    elif not differences and not (request.amount or request.room):
        result.message += "目前金额匹配未发现差异；这不代表凭证完整或本月已经完成关账。"
    if request.action in {"note", "explain", "withdraw_note"}:
        result.message += "请先选择一笔，说明才会绑定到具体明细。"
    return result
