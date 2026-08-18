"""助手编排 —— 分类 → 校验 → 派发 → 措辞 → 审计。单发意图分类，无 agentic 循环。

铁律落地：
- 意图 reject/clarify、白名单外 tool、周期含糊 → 拒答/回显，绝不 force-fit（#1）。
- OTA 单强制附盲区声明，不给「是/否系统改的」清晰判决（#3）。
- 措辞 LLM 挂了 → 纯确定性结果 + narration_degraded（#8）。
- 每次问答写审计（护栏④）。
"""
from __future__ import annotations

import logging

from app.services.assistant.intent import classify_question
from app.services.assistant.narrate import narrate
from app.services.assistant.tools import (
    ToolParamError,
    UnknownToolError,
    dispatch,
    validate_tool_call,
)
from app.services.audit import log_action

logger = logging.getLogger(__name__)

_CHANNEL_LABELS = {
    "ctrip": "携程", "qunar": "去哪儿", "zhixing": "智行", "tongcheng": "同程",
    "meituan_hotel": "美团酒店", "meituan_homestay": "美团民宿", "douyin": "抖音",
    "fliggy": "飞猪", "offline": "线下渠道", "self_acquired": "自来客",
    "trial_stay": "试住", "self_used": "自住",
}

_OTA_DISCLAIMER = (
    "此单为平台直连（携程/美团等），平台/同步侧的改动不写系统日志，"
    "我只能确认后台里的手动操作，无法确认平台侧是否改过。"
)


def _base(kind: str) -> dict:
    return {
        "kind": kind,
        "summary": "",
        "timeline_text": None,
        "timeline_rows": None,
        "data": None,
        "orders": None,
        "candidates": None,
        "disclaimers": [],
        "narration_degraded": False,
        "clarify_question": None,
        "reject_reason": None,
    }


def _reject(reason: str) -> dict:
    a = _base("reject")
    a["summary"] = reason
    a["reject_reason"] = reason
    return a


def _clarify(question: str) -> dict:
    a = _base("clarify")
    a["summary"] = question
    a["clarify_question"] = question
    return a


def _metric_fallback(tool: str, payload: dict) -> str:
    label = payload.get("period_label", "")
    if tool == "order_count":
        return f"{label}共 {payload.get('order_count', 0)} 单。"
    if tool == "period_metrics":
        return (f"{label}：{payload.get('order_count', 0)} 单，"
                f"营业额 ¥{payload.get('revenue', 0)}，净收入 ¥{payload.get('net_revenue', 0)}。")
    if tool == "owner_earnings":
        return f"{label}各房东实际到手合计 ¥{payload.get('total_actual_owner_amount', 0)}，明细见下方。"
    if tool == "cleaning_workload":
        return f"{label}共打扫 {payload.get('total_rooms_cleaned', 0)} 间。"
    if tool == "room_revenue":
        return f"{label}各房营收见下方明细。"
    if tool == "today_snapshot":
        return "今日概况见下方数据。"
    return "结果见下方数据。"


def _format_metric_facts(tool: str, payload: dict) -> str:
    """Turn verified structured values into readable facts for narration; never expose raw JSON."""
    if tool == "owner_earnings":
        lines = [f"{payload.get('period_label', '')}房东结算实际到手："]
        for row in payload.get("owners") or []:
            statuses = "、".join(f"{key}{value}笔" for key, value in (row.get("status_counts") or {}).items())
            lines.append(
                f"{row.get('owner_name')}：分成¥{row.get('owner_amount', 0)}，"
                f"扣除支出¥{row.get('deducted_expenses', 0)}，实际到手¥{row.get('actual_owner_amount', 0)}"
                + (f"（{statuses}）" if statuses else "") + "。"
            )
        return "\n".join(lines)
    if tool != "period_metrics":
        return _metric_fallback(tool, payload)

    lines = [
        f"{payload.get('period_label', '')}合计：{payload.get('order_count', 0)}单，"
        f"营业额¥{payload.get('revenue', 0)}，平台佣金¥{payload.get('commission', 0)}，"
        f"净收入¥{payload.get('net_revenue', 0)}，间夜{payload.get('room_nights', 0)}。"
    ]
    monthly = payload.get("monthly_breakdown") or []
    if monthly:
        lines.append("按月份：")
        lines.extend(
            f"{row.get('period_label')}：{row.get('order_count', 0)}单，"
            f"营业额¥{row.get('revenue', 0)}，净收入¥{row.get('net_revenue', 0)}。"
            for row in monthly
        )
    channels = payload.get("by_channel") or {}
    if channels:
        lines.append("按渠道：")
        lines.extend(
            f"{_CHANNEL_LABELS.get(code, code)}：{row.get('order_count', 0)}单，营业额¥{row.get('revenue', 0)}。"
            for code, row in channels.items()
        )
    return "\n".join(lines)


async def _narrate_or_fallback(question: str, facts_text: str, disclaimers: list[str], fallback: str) -> tuple[str, bool]:
    try:
        text = await narrate(question, facts_text, disclaimers)
        return text, False
    except Exception as e:  # 措辞失败（含 AssistantLLMError）绝不拖垮可用性
        logger.warning("assistant narrate degraded: %s", e)
        joined = fallback
        if disclaimers:
            joined = joined + "\n" + "\n".join(disclaimers)
        return joined + "\n（AI 措辞暂不可用，以上为系统直出结果）", True


async def _forensics_answer(question: str, payload: dict) -> dict:
    if not payload.get("found"):
        a = _base("forensics")
        a["summary"] = "没找到符合条件的订单。换个说法，或补个日期/房号我再查。"
        return a

    if payload.get("ambiguous"):
        a = _clarify(f"找到 {payload['matched']} 个符合的订单，你说的是哪一单？（补个日期或房号我就能定位）")
        a["candidates"] = payload.get("candidates")
        return a

    orders = payload["orders"]
    disclaimers: list[str] = []
    if any(o.get("ota_blind_spot") for o in orders):
        disclaimers.append(_OTA_DISCLAIMER)

    facts_parts = []
    for o in orders:
        head = f"订单 {o['order_id']}（{o.get('guest_name')}，{o.get('channel')}）操作记录："
        facts_parts.append(head + "\n" + (o.get("timeline_text") or "（无操作记录）"))
    facts_text = "\n\n".join(facts_parts)

    summary, degraded = await _narrate_or_fallback(
        question, facts_text, disclaimers,
        fallback="已找到该单，操作记录见下方时间线。",
    )
    a = _base("forensics")
    a["summary"] = summary
    a["orders"] = orders
    a["timeline_text"] = "\n\n".join(o.get("timeline_text") or "" for o in orders)
    a["timeline_rows"] = [r for o in orders for r in (o.get("timeline_rows") or [])]
    a["disclaimers"] = disclaimers
    a["narration_degraded"] = degraded
    return a


async def _metrics_answer(question: str, tool: str, payload: dict) -> dict:
    fallback = _metric_fallback(tool, payload)
    facts = _format_metric_facts(tool, payload)
    summary, degraded = await _narrate_or_fallback(question, facts, [], fallback)
    a = _base("metrics")
    a["summary"] = summary
    a["data"] = payload
    a["narration_degraded"] = degraded
    return a


async def _audit_ask(db, current_user, intent) -> None:
    try:
        # 问题正文和参数值可能含姓名、订单号、手机号等敏感信息；审计只保留
        # 路由元数据，足够统计能力使用情况，也不会形成第二份业务数据副本。
        param_keys = sorted(intent.params.keys()) if isinstance(intent.params, dict) else []
        await log_action(
            db, current_user.get("user_id"), "assistant.ask",
            resource_type="assistant", resource_id=None,
            notes=f"action={intent.action} | tool={intent.tool} | param_keys={param_keys}",
        )
    except Exception:
        logger.exception("assistant audit failed")


async def answer_question(
    db, question: str, current_user: dict, history: list[dict] | None = None,
) -> dict:
    """助手主流程。意图分类失败（LLM 降级）会向上抛 AssistantLLMError，端点转 503。"""
    intent = await classify_question(question, history) if history else await classify_question(question)
    await _audit_ask(db, current_user, intent)

    if intent.action == "reject":
        return _reject(intent.reject_reason or "这个我暂时查不了。")
    if intent.action == "clarify":
        return _clarify(intent.clarify_question or "我没太明白，你想查什么？")

    # dispatch
    try:
        call = validate_tool_call(intent.tool, intent.params)
    except UnknownToolError:
        return _reject("这个我暂时查不了。")
    except ToolParamError:
        return _clarify("我没太明白你想查什么，能再说具体点吗？")

    try:
        payload = await dispatch(db, call, current_user)
    except ToolParamError as e:
        return _clarify(f"时间范围我没弄准，你是指哪段时间？（{e}）")

    if call.spec.kind == "forensics":
        return await _forensics_answer(question, payload)
    return await _metrics_answer(question, call.tool, payload)
