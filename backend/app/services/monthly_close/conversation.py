"""Read-only conversational progress built from current monthly-close evidence."""

import re
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.models.monthly_close import MonthlyCloseDocument
from app.services.billing_recon.parser import BillParseError
from app.services.monthly_close.amount_summary import AmountSummary, read_amount_summary
from app.services.monthly_close.amount_request import amount_request, asks_amount, unsupported_amount_scope
from app.services.monthly_close.cleaning_work_log import (
    ServiceStatementError,
    compare_cleaning_work_log,
    parse_cleaning_work_log,
)
from app.services.monthly_close.cost_responsibility import (
    asks_cleaning_responsibility,
    cleaning_responsibility_message,
)

VERSION = "monthly-close-progress-chat/v3"
LABELS = {
    "cleaning_statement": "保洁",
    "linen_statement": "布草洗涤",
    "utility_expense": "水电支出",
    "ota_statement": "OTA平台账单",
    "operating_expenses": "运营支出",
}
ALIASES = {
    "cleaning_statement": r"保洁|清洁|打扫",
    "linen_statement": r"布草|洗涤|床单|被套",
    "utility_expense": r"水电|电费|水费|充值",
    "ota_statement": r"OTA|携程|美团|抖音|去哪儿|同程|飞猪|平台账单",
    "operating_expenses": r"运营支出|日耗|耗材|杂费|采购",
}
STEP_ALIASES = {
    "order_integrity": r"订单|入住|退房",
    "exception_clearance": r"异常|阻塞",
    "preflight": r"结算前|预检",
    "settlement_review": r"业主结算|结算单|业主收入",
    "owner_confirmation": r"业主确认",
}
STEPS = {
    "source_collection": "资料收集",
    "order_integrity": "订单完整性",
    "service_fees": "保洁、布草与服务费",
    "utilities": "水电与运营支出",
    "ota_statements": "OTA平台对账",
    "exception_clearance": "异常处理",
    "preflight": "结算前检查",
    "settlement_review": "业主结算复核",
    "owner_confirmation": "业主确认",
}
GUIDANCE = {
    "cleaning_statement": "提供保洁打扫明细；核对费用还需要供应商费用依据。",
    "linen_statement": "上传本月布草洗涤对账表，核对日期、房间、数量及费用。",
    "utility_expense": "上传本月水电账单或充值记录，核对房间、日期和支出。",
    "ota_statement": "上传本月携程、美团等实际使用平台的结算账单。",
    "operating_expenses": "上传本月采购、日耗和其他运营支出明细。",
}
FORMAT_FIELDS = {
    "cleaning_statement": "日期、房间号、正常或续住打扫；核对费用还需金额或单价依据",
    "linen_statement": "日期、房间号、布草种类、数量和费用",
    "utility_expense": "房间号、日期、费用或充值金额",
    "ota_statement": "平台、订单号、结算日期、收入及扣款明细",
    "operating_expenses": "日期、支出项目、金额和房间号（如有）",
}


class ProgressSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: str
    label: str
    state: str
    detail: str
    next_step: str
    document_count: int


class ProgressStep(BaseModel):
    model_config = ConfigDict(extra="forbid")
    step_key: str
    label: str
    status: str
    blocking_count: int


class ProgressDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")
    subject: str
    cause: str
    next_step: str
    issue_code: str | None = None
    order_id: str | None = None


class ProgressFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["month_progress"] = "month_progress"
    billing_month: str
    request_text: str
    focus: str | None = None
    message: str
    sources: list[ProgressSource] = Field(default_factory=list)
    steps: list[ProgressStep] = Field(default_factory=list)
    cleaning_summary: str | None = None
    detail_items: list[ProgressDetail] | None = None
    query_mode: str | None = None
    amount_summary: AmountSummary | None = None
    detail_total: int | None = None
    detail_evidence_hash: str | None = None


def _detail_next_step(issue):
    if issue.get("code") == "missing_platform_order_id":
        return (
            "先在订单管理核对这笔订单，再查平台原始订单，取得真实平台订单号。"
            f"然后直接告诉我“订单 {issue.get('order_id', 'ORD-…')} 的平台订单号是 …”，"
            "我会展示补充方案，确认后保存并重新核对。我不会代填；系统 ORD 编号不能代替平台订单号。"
        )
    return str(issue.get("next_step", "查看对应核对事项"))


def progress_request(text):
    """Explicit topic switches take priority over a preceding write plan.

    This router can only request a read; it can never approve or generate a write.
    Unrecognized text continues through the existing assistant boundary.
    """
    if amount := amount_request(text):
        return amount["focus"]
    value = re.sub(r"\s+", "", text)
    if re.search(
        r"删除|删掉|移除|补齐|补录|执行|批准|确认处理|恢复|撤销|取消方案|改成|修改|直接关账",
        value,
    ):
        return None
    broad = bool(
        re.search(
            r"还.{0,12}(?:没|差|缺|要|什么|哪些)|其他|剩下|接下来|下一步|然后|整个月|整月|都.{0,6}(?:完|对好)|结束|做完|弄完|没.{0,4}(?:对|核|完)|未.{0,4}(?:对|核|完)|清单",
            value,
        )
    )
    found = [
        key
        for key, pattern in ALIASES.items()
        if re.search(pattern, value, re.IGNORECASE)
    ]
    if "cleaning_statement" in found and re.search(r"费用|单价|金额|供应商账单", value):
        return "cleaning_statement"
    if (
        "cleaning_statement" in found
        and len(found) == 1
        and not re.search(r"其他|接下来|下一步|然后|整月|整个月|除了", value)
    ):
        return None
    found += [key for key, pattern in STEP_ALIASES.items() if re.search(pattern, value)]
    if broad:
        return (
            "all"
            if not found or re.search(r"其他|整月|整个月|除了", value)
            else found[0]
        )
    if found and found[0] != "cleaning_statement":
        return found[0]
    if re.search(r"账单|月结|进度|状态", value):
        return "all"
    return None


async def read_progress(db, cycle, projection, text, focus, *, mode=None, expense_categories=None):
    base = {
        "billing_month": cycle.billing_month,
        "request_text": text[:4000],
        "focus": focus,
        "query_mode": mode,
    }
    month_text = text
    for number, name in reversed(
        list(
            enumerate(
                [
                    "一",
                    "二",
                    "三",
                    "四",
                    "五",
                    "六",
                    "七",
                    "八",
                    "九",
                    "十",
                    "十一",
                    "十二",
                ],
                1,
            )
        )
    ):
        month_text = month_text.replace(name + "月", str(number) + "月")
    month_text = re.sub(r"(20\d{2})[-/](\d{1,2})(?!\d)", r"\1年\2月", month_text)
    requested = re.search(r"(?:(20\d{2})年)?(\d{1,2})月", month_text)
    if requested and (
        int(requested.group(2)) != int(cycle.billing_month[5:])
        or (requested.group(1) and requested.group(1) != cycle.billing_month[:4])
    ):
        return ProgressFacts(
            **base,
            message=f"当前打开的是 {cycle.billing_month}。请先切换到你问的月份，我再按那个月的实际资料核对。",
        )
    amount = amount_request(text)
    if (mode == "amount_summary" or asks_amount(text)) and unsupported_amount_scope(text):
        base.update(query_mode="amount_summary")
        return ProgressFacts(**base, message="目前聊天金额查询只能提供当前整月的费用分类合计，暂不支持这次的额外筛选或收入类统计。我不能把整月支出当成你要的结果。请在财务管理中按对应条件核对。")
    if mode == "amount_summary" or amount:
        selected_focus = amount["focus"] if amount else focus
        categories = amount.get("expense_categories") if amount else expense_categories
        summary, message = await read_amount_summary(db, cycle.billing_month, selected_focus, categories)
        base.update(focus=selected_focus, query_mode="amount_summary")
        return ProgressFacts(**base, message=message, amount_summary=summary)
    if mode == "cost_responsibility" or asks_cleaning_responsibility(text):
        base.update(focus="cleaning_statement", query_mode="cost_responsibility")
        return ProgressFacts(**base, message=cleaning_responsibility_message())
    work_summary = None
    cleaning_complete = False
    visible = [
        doc.document_id
        for source in projection.sources
        if source.source_type == "cleaning_statement"
        for doc in source.documents
    ]
    reports = []
    if visible:
        documents = await db.scalars(
            select(MonthlyCloseDocument)
            .where(
                MonthlyCloseDocument.document_id.in_(visible),
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.is_active.is_(True),
            )
            .options(undefer(MonthlyCloseDocument.content))
        )
        for document in documents:
            if (
                not document.content
                or sha256(document.content).hexdigest() != document.sha256
            ):
                continue
            try:
                entries = parse_cleaning_work_log(
                    document.content, document.filename, cycle.billing_month
                )
                if entries is not None:
                    reports.append(
                        await compare_cleaning_work_log(
                            db, entries, cycle.billing_month, document.document_id
                        )
                    )
            except (BillParseError, ServiceStatementError):
                continue
        # Overlapping workbooks are separate evidence; never sum them into a fictitious total.
        if len(reports) == 1:
            report = reports[0]
            cleaning_complete = not report.differences
            work_summary = f"保洁打扫记录已对应 {report.matched_count} 次，" + (
                "这份表已核对完成。"
                if cleaning_complete
                else f"还有 {len(report.differences)} 项差异。"
            )
        elif reports:
            work_summary = f"有 {len(reports)} 份保洁打扫表，请指定一份查看，避免把重叠记录重复计算。"
    sources = []
    descriptions = {
        "missing": "还没收到资料",
        "processing": "已收到，正在处理",
        "needs_action": "已收到，仍需识别或核对",
        "blocked": "仍有事项待处理",
        "completed": "资料已处理",
        "not_applicable": "已标记本月不适用",
    }
    for source in projection.sources:
        detail = descriptions[source.state]
        next_step = GUIDANCE[source.source_type]
        if source.state == "processing":
            next_step = "资料正在处理，完成后再查看结果，无需重复上传。"
        elif source.state == "completed":
            next_step = "已有资料已处理，接下来按本月进度继续核对和复核。"
        elif source.state == "not_applicable":
            next_step = "这类资料本月无需补充。"
        elif source.documents and source.state in {"needs_action", "blocked"}:
            next_step = "先查看已有文件的识别结果和待处理事项，无需重复上传同一份表。"
        if source.source_type == "cleaning_statement" and work_summary:
            detail = work_summary
            if cleaning_complete and source.state not in {
                "completed",
                "not_applicable",
            }:
                detail += "费用资料尚未完成核对。"
        sources.append(
            ProgressSource(
                source_type=source.source_type,
                label=LABELS[source.source_type],
                state=source.state,
                detail=detail,
                next_step=next_step,
                document_count=len(source.documents),
            )
        )
    steps = [
        ProgressStep(
            step_key=row["step_key"],
            label=STEPS[row["step_key"]],
            status=row["status"],
            blocking_count=row["blocking_count"],
        )
        for row in projection.workflow_evidence
    ]
    detail_items = None
    detail_total = None
    detail_evidence_hash = None
    if focus != "all":
        source = next((row for row in sources if row.source_type == focus), None)
        if source:
            detail = source.detail.rstrip("。.!！?？；; ")
            message = f"{source.label}：{detail}。当前已归档 {source.document_count} 份资料。{source.next_step}"
            if mode == "format" or re.search(r"格式|哪些列|什么字段|什么内容", text):
                message = f"{source.label}请发 Excel 文件（.xls 或 .xlsx）。表里应保留{FORMAT_FIELDS[source.source_type]}。可以直接发供应商原表；上传后先查看识别结果，缺少字段或对应不准的地方再补充。"
        else:
            step = next((row for row in steps if row.step_key == focus), None)
            if step:
                state = (
                    "已确认完成"
                    if step.status == "confirmed"
                    else f"还有 {step.blocking_count} 项待处理"
                    if step.blocking_count
                    else "检查未发现阻塞项，尚待确认"
                )
                message = f"{step.label}：{state}。请查看本步骤的核对事项。"
                if getattr(projection, "actor_role", None) == "admin":
                    from app.services.monthly_close.evidence import (
                        build_role_workflow_evidence,
                    )

                    evidence = await build_role_workflow_evidence(
                        db, cycle, actor_role="admin"
                    )
                    selected_step = next(
                        (item for item in evidence if item["step_key"] == focus), {}
                    )
                    issues = selected_step.get("issues", [])
                    detail_total = len(issues)
                    detail_evidence_hash = selected_step.get("evidence_hash")
                    # Only bounded previews belong in durable chat. Full details are read
                    # from the authorized, evidence-pinned pagination endpoint.
                    detail_items = [
                        ProgressDetail(
                            subject=str(item.get("subject", "待处理事项"))[:160],
                            cause=str(item.get("cause", "尚待核实"))[:500],
                            next_step=_detail_next_step(item)[:300],
                            issue_code=item.get("code"),
                            order_id=item.get("order_id"),
                        )
                        for item in issues[:3]
                    ]
                    if focus == "preflight":
                        fee_notes = selected_step.get("summary", {}).get("fee_explanations", [])
                        pending_fees = [n for n in fee_notes if n.get("code") == "service_fee_missing"]
                        explained_fees = [n for n in fee_notes if n.get("code") != "service_fee_missing"]
                        if not issues:
                            message = "结算前检查：当前未发现阻断问题。"
                            if pending_fees:
                                message += f"还有 {len(pending_fees)} 笔服务费待入账，生成结算方案时会列出补录，确认结算前需完成。"
                            if explained_fees:
                                message += f"\n另有 {len(explained_fees)} 笔续住、跨月、公司承担或免收说明，这些不作为新的待办。"
                                for note in explained_fees[:2]:
                                    who = " · ".join(filter(None, [note.get("guest_name"), note.get("room_name")]))
                                    message += f"\n{who}：{note['message']}；当前登记 {note.get('amount') or '0.00'} 元。"
                            message += "\n完整明细可在业主结算页的结算检查中展开查看。"
                    if detail_items:
                        message = f"{step.label}：{state}。\n"
                        shown = 0
                        for index, item in enumerate(detail_items[:3], 1):
                            paragraph = (
                                f"{index}. {item.subject[:80]}：{item.cause[:100].rstrip('。；')}。\n"
                                f"处理方式：{item.next_step[:200]}\n"
                            )
                            if len(message) + len(paragraph) > 470:
                                break
                            message += paragraph
                            shown += 1
                        if shown < detail_total:
                            message += f"共 {detail_total} 项，可展开分页查看全部事项、完整原因与处理建议。"

            else:
                message = "当前权限下没有这类资料。"
    else:
        pending = [
            row for row in sources if row.state not in {"completed", "not_applicable"}
        ]
        message = f"{cycle.billing_month} 的整月对账" + (
            "已经完成。" if cycle.status == "completed" else "还没有完成。"
        )
        settlement_status=getattr(projection.final_review,'settlement_status_message','')
        if settlement_status:
            message += '\n'+settlement_status
        if work_summary:
            message += "\n" + work_summary
        visible_pending = [
            row
            for row in pending
            if row.source_type != "cleaning_statement" or not cleaning_complete
        ]
        if visible_pending:
            message += (
                "\n还需处理："
                + "；".join(
                    f"{row.label}（{descriptions[row.state]}）"
                    for row in visible_pending
                )
                + "。"
            )
        if cleaning_complete and any(
            row.source_type == "cleaning_statement" for row in pending
        ):
            message += "\n打扫次数已对齐；保洁费用还需供应商费用依据继续核对。"
        remaining = [
            row.label
            for row in steps
            if row.status != "confirmed"
            and row.step_key
            not in {"source_collection", "service_fees", "utilities", "ota_statements"}
        ]
        if remaining:
            message += "\n后续还有：" + "、".join(remaining) + "。"
        next_source = next(
            (row for row in pending if row.state == "missing"),
            pending[0] if pending else None,
        )
        if next_source:
            message += "\n建议先：" + next_source.next_step
        elif cycle.status != "completed":
            message += "\n下一步：" + projection.recommended_action.label + "。"
    if mode == "capabilities":
        message = (
            "你可以直接用日常说法告诉我需求，也可以上传 Excel（.xls / .xlsx）。\n"
            "我能查本月缺哪些资料、解释表格要哪些字段、查看订单及月结步骤卡在哪里、调查保洁费用依据。\n"
            "对于打扫记录，可以按原表对照，提出补齐、删除多余记录或恢复已删除记录的方案；你可以继续改范围，确认后才执行。\n"
            "缺失平台订单号也可在聊天里补充：提供系统订单编号和真实平台单号，核对方案后确认保存。查询结果可以复制或下载作交接；之后记录变化需要重新查询。\n"
            "当前不会代替你批准结算，也不支持联网搜索、定时通知或向别人发送消息。可以先问“本月还缺什么，先做哪一步”。"
        )
    return ProgressFacts(
        **base,
        message=message,
        sources=sources,
        steps=steps,
        cleaning_summary=work_summary,
        detail_items=detail_items,
        detail_total=detail_total,
        detail_evidence_hash=detail_evidence_hash,
    )
