"""Bounded model investigation over typed evidence, never executable model prose.

Provider inputs contain ordered business concepts, selectors, and aggregate facts.
Names, workbook cells, free-form notes, URLs and database identifiers never leave
this boundary. Model-selected findings must exist in the server-owned evidence
catalog; accounting calculations and correction builders remain deterministic.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from typing import Literal

from openai import AsyncOpenAI
from pydantic import BaseModel, ConfigDict, Field

from app.core.config import settings
from app.services.monthly_close.cleaning_investigation import CleaningInvestigationFacts

AGENT_VERSION = "monthly-close-investigation-agent/v1"
MAX_STEPS = 5
TIMEOUT = 40
# Longest match first; discarded text cannot accidentally become a provider instruction.
BUSINESS_WORDS = sorted(
    {
        "保洁",
        "打扫",
        "清洁",
        "阿姨",
        "差异",
        "对不上",
        "不一致",
        "为什么",
        "怎么回事",
        "查一下",
        "查清楚",
        "核实",
        "核对",
        "继续",
        "这笔",
        "上一笔",
        "下一笔",
        "第二笔",
        "额外",
        "加做",
        "一次",
        "没有",
        "不是",
        "可能",
        "是不是",
        "确定",
        "撤回",
        "说明",
        "重复",
        "修订",
        "新版",
        "旧版",
        "账单",
        "原件",
        "订单",
        "换房",
        "续住",
        "退房",
        "任务",
        "完成",
        "审核",
        "凭证",
        "缺少",
        "还要",
        "怎么处理",
        "下一步",
        "方案",
        "草稿",
        "生成",
        "建议",
        "原因",
        "金额",
        "费用",
        "单价",
        "数量",
        "多收",
        "少收",
        "漏记",
        "全部",
        "清单",
        "房间",
        "房号",
        "日期",
        "本月",
        "上次",
        "重新",
        "已经",
        "人工",
        "系统",
        "供应商",
        "没做",
        "做了",
        "不对",
        "先看",
        "第一笔",
        "第三笔",
        "多算",
        "少算",
        "改账",
        "批准",
        "执行",
        "删除",
        "直接",
        "不要",
        "确认",
        "补充",
        "材料",
        "收费",
    },
    key=len,
    reverse=True,
)
_WORDS = re.compile("|".join(map(re.escape, BUSINESS_WORDS)))


def business_question(text: str) -> list[str]:
    """Preserve question order and negation without retaining unknown user prose."""
    return _WORDS.findall(unicodedata.normalize("NFKC", text))[:100]


class AgentDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tool: Literal["read_amounts", "read_sources", "read_orders", "read_tasks", "finish"]
    finding_ids: list[str] = Field(default_factory=list, max_length=12)


async def call_model(messages: list[dict]) -> str:
    async with AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
        timeout=8,
        max_retries=0,
    ) as client:
        result = await client.chat.completions.create(
            model="deepseek-chat",
            max_tokens=350,
            temperature=0,
            response_format={"type": "json_object"},
            messages=messages,
        )
    return result.choices[0].message.content or ""


def evidence_catalog(facts: CleaningInvestigationFacts) -> dict[str, list[dict]]:
    case = facts.selected
    if not case:
        return {}
    catalog: dict[str, list[dict]] = {
        key: [] for key in ("read_amounts", "read_sources", "read_orders", "read_tasks")
    }

    def add(tool, code, text, refs):
        catalog[tool].append({"id": code, "text": text, "evidence_refs": refs})

    source_refs = [f"source:{row.line_id}" for row in case.sources]
    expense_refs = [f"expense:{row.expense_id}" for row in case.expenses]
    add("read_amounts", "amounts", case.summary, source_refs + expense_refs)
    if case.status == "amount_mismatch":
        add(
            "read_amounts",
            "price_unverified",
            "差额已经算清，但现有记录不能证明供应商金额或系统金额哪一个应当修改；需要核对约定单价和实际服务次数。",
            source_refs + expense_refs,
        )
    if case.related_sources:
        add(
            "read_sources",
            "overlapping_sources",
            "多份有效文件含有相同业务明细。请确认是新增服务还是整份修订，不能直接判定重复收费。",
            source_refs + [f"source:{row.line_id}" for row in case.related_sources],
        )
    else:
        add(
            "read_sources",
            "source_rows",
            "已核对当前有效原表行；没有发现跨文件的相同业务键。这不能排除不同写法的重复记录。",
            source_refs,
        )
    if len(case.sources) > 1:
        add(
            "read_sources",
            "grouped_rows",
            "这笔由同一业务键下的多行金额汇总，应核实这些行是否代表独立服务。",
            source_refs,
        )
    if case.orders:
        refs = [f"order:{row.order_id}" for row in case.orders]
        if any(row.relationship == "room_date_candidate" for row in case.orders):
            add(
                "read_orders",
                "candidate_order",
                "找到同房同日期的订单候选，尚不能证明它与本次收费一一对应。",
                refs,
            )
        else:
            add(
                "read_orders",
                "linked_orders",
                "已查到关联订单，可结合入住、退房日期核实本次服务归属。",
                refs,
            )
        if any(row.stay_group_id for row in case.orders):
            add(
                "read_orders",
                "stay_group",
                "订单存在续住关联；需区分退房保洁与续住期间额外服务，续住关系本身不能证明应该多收一次。",
                refs,
            )
    else:
        add(
            "read_orders",
            "missing_order",
            "未找到可关联订单。请补充本次保洁对应的房间、服务日期或订单凭证。",
            source_refs,
        )
    if case.tasks:
        add(
            "read_tasks",
            "task_not_price_proof",
            "已查到关联保洁任务；完成或审核状态只能作为服务线索，不能单独证明收费金额正确。",
            [f"task:{row.task_id}" for row in case.tasks],
        )
    else:
        add(
            "read_tasks",
            "missing_task",
            "未查到关联保洁任务。请补充派工或服务完成凭证，不能仅凭缺少任务就认定没有打扫。",
            source_refs,
        )
    if facts.note_code:
        add(
            "read_sources",
            "employee_lead",
            "员工补充说明仍是待核实线索，需要服务记录或修订原件支持。",
            source_refs,
        )
    if case.detail_limited:
        add(
            "read_orders",
            "limited",
            "关联记录超过展示上限，当前依据不完整；请缩小范围后继续核对。",
            source_refs,
        )
    return catalog


async def reason_over_evidence(
    facts: CleaningInvestigationFacts, question: list[str]
) -> tuple[list[dict], list[str], str]:
    catalog = evidence_catalog(facts)
    if not catalog:
        return [], [], "not_needed"
    fallback = [item for items in catalog.values() for item in items]
    if (
        not settings.MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED
        or not settings.DEEPSEEK_API_KEY
    ):
        return fallback, list(catalog), "disabled"
    # Only codes, booleans and counts go to the provider; no raw financial IDs or prose.
    case = facts.selected
    messages = [
        {
            "role": "system",
            "content": (
                "你是月结保洁调查员。根据按原顺序提取的业务概念选择下一步只读调查。"
                '每次只返回JSON：{"tool":"read_amounts|read_sources|read_orders|read_tasks|finish","finding_ids":[]}。'
                "四个工具分别核实差额、原件版本、订单续住、保洁任务。先核实金额和原件，再按问题查订单和任务；不能重复调用。read工具的finding_ids必须为[]。"
                "finish时只能选择已经查到的finding_ids，至少包含amounts。不能把猜测或员工说明当成凭证，不能修改金额或发起审批。"
            ),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "question_concepts": question,
                    "selected": True,
                    "status": case.status,
                    "note_code": facts.note_code,
                },
                ensure_ascii=False,
            ),
        },
    ]
    seen: list[str] = []
    found: dict[str, dict] = {}
    try:
        async with asyncio.timeout(TIMEOUT):
            for _ in range(MAX_STEPS):
                decision = AgentDecision.model_validate_json(await call_model(messages))
                if decision.tool == "finish":
                    if (
                        not {"read_amounts", "read_sources"}.issubset(seen)
                        or "amounts" not in decision.finding_ids
                        or any(key not in found for key in decision.finding_ids)
                    ):
                        raise ValueError("ungrounded model findings")
                    # Always retain unresolved evidence and caveats, even if the model omits them.
                    ordered = list(
                        dict.fromkeys(
                            decision.finding_ids + [item["id"] for item in fallback]
                        )
                    )
                    index = {item["id"]: item for item in fallback}
                    return [index[key] for key in ordered], seen, "model"
                if decision.tool in seen or any(
                    key not in found for key in decision.finding_ids
                ):
                    raise ValueError("invalid or repeated tool")
                seen.append(decision.tool)
                items = catalog[decision.tool]
                found.update({item["id"]: item for item in items})
                messages.extend(
                    [
                        {"role": "assistant", "content": decision.model_dump_json()},
                        {
                            "role": "user",
                            "content": json.dumps(
                                {
                                    "tool_result": decision.tool,
                                    "findings": [
                                        {"id": item["id"], "meaning": item["text"]}
                                        for item in items
                                    ],
                                    "record_count": len(items),
                                }
                            ),
                        },
                    ]
                )
    except asyncio.CancelledError:
        raise
    except Exception:
        return fallback, seen, "fallback"
    return fallback, seen, "fallback"


async def correction_draft(db, cycle, facts):
    from sqlalchemy import select
    from app.models.expense import Expense
    from app.services.monthly_close.adapters import adapt_service_source
    from app.services.monthly_close.cleaning_investigation import (
        CorrectionDraft,
        CorrectionChange,
    )
    from app.services.monthly_close.source_contract import source_digest

    adapter = await adapt_service_source(db, cycle)
    if adapter.state != "ready" or not adapter.commands:
        return CorrectionDraft(
            state="needs_evidence" if adapter.issues else "no_change",
            message=(
                "订单规则仍有待确认事项，暂不能生成确定性修正；请补齐服务日期、订单归属或收费凭证。"
                if adapter.issues
                else "当前订单规则没有可生成的系统服务费变更。请先核实供应商单价、服务次数或修订原件，不能把账单金额直接写入系统。"
            ),
        )
    fees = [
        fee for command in adapter.commands for fee in command.after.get("fees", [])
    ]
    if len(fees) > 100:
        return CorrectionDraft(
            state="limited",
            message="本月变更超过100项，请打开服务费方案逐项审核；此处未展示不完整金额。",
        )
    expense_ids = {
        eid
        for command in adapter.commands
        for eid in command.before.get("active_expense_ids", [])
    }
    expenses = (
        list(
            await db.scalars(
                select(Expense).where(
                    Expense.expense_id.in_(expense_ids), Expense.is_deleted.is_(False)
                )
            )
        )
        if expense_ids
        else []
    )
    changes = []
    for fee in fees:
        matches = [
            item
            for item in expenses
            if item.order_id == fee["order_id"]
            and item.room_id == fee["room_id"]
            and item.category.value == fee["category"]
        ]
        changes.append(
            CorrectionChange(
                order_id=fee["order_id"],
                room_id=fee["room_id"],
                category=fee["category"],
                before_amount=format(matches[0].amount, ".2f")
                if len(matches) == 1
                else None,
                after_amount=fee["amount"],
                before_date=matches[0].expense_date.isoformat()
                if len(matches) == 1
                else None,
                after_date=fee["expense_date"],
                before_payer=matches[0].payer.value if len(matches) == 1 else None,
                after_payer=fee["payer"],
            )
        )
    selected = facts.selected
    related = bool(
        selected
        and any(
            item.room_id == selected.room_id
            and item.order_id
            in {
                order.order_id
                for order in selected.orders
                if order.relationship != "room_date_candidate"
            }
            for item in changes
        )
    )
    from decimal import Decimal

    return CorrectionDraft(
        state="ready",
        message=(
            "这是本月按订单与费率规则计算的服务费修正草稿，包含下列全部变更。正式方案会重新取数并展示完整影响，再由管理员批准、执行和复核。"
            + (
                "其中有与本笔关联的订单变更，但仍需核实供应商差异原因。"
                if related
                else "这些变更尚不能解释所选供应商差异，请分别核对。"
            )
        ),
        changes=changes,
        amount_impact=format(
            sum(
                (command.amount_impact for command in adapter.commands), Decimal("0.00")
            ),
            ".2f",
        ),
        evidence_hash=source_digest(
            [command.model_dump(mode="python") for command in adapter.commands]
        ),
        case_related=related,
    )
