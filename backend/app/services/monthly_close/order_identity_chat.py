"""Administrator-confirmed filling of missing OTA identity, without model writes."""

import json
import re
from hashlib import sha256
from typing import Literal

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select

from app.models.audit_log import AuditLog
from app.models.order import Order
from app.services.audit import log_action_tx
from app.services.monthly_close.adapters import (
    OTA_PLATFORM_CHANNELS,
    order_integrity_snapshot,
)
from app.services.monthly_close.cleaning_work_import import _admin
from app.services.monthly_close.financial_lock import acquire_month_financial_lock
from app.services.monthly_close.semantic_agent import month_scope_question
from app.services.monthly_close.workflow import (
    MonthlyCloseConflict,
    require_cycle_writable,
)
from app.services.order_identity_lock import check_platform_identity_available

VERSION = "monthly-close-order-identity-chat/v1"
CONFIRM = {"确认执行", "确认", "通过", "执行这个方案", "按这个方案执行"}
CANCEL = {"取消", "取消方案", "先不执行", "先别执行", "不要执行"}
VALUE_PATTERN = re.compile(
    r"平台(?:订单号|单号)\s*(?:是|为|[:：]|填入|填|改为|补为|补上)?\s*[‘’“\"']?([A-Za-z0-9][^\s\u4e00-\u9fff，。；;‘’“”\"']*)"
)
VALID_VALUE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,99}")


class OrderIdentityFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["order_identity_chat"] = "order_identity_chat"
    billing_month: str
    request_text: str
    state: Literal["clarification", "proposal", "completed", "cancelled", "conflict"]
    message: str
    order_id: str | None = None
    platform_order_id: str | None = None
    preview_hash: str | None = None
    remaining_count: int | None = None


def wants_identity(text):
    # Routing is deliberately broader than authorization: even negated or
    # malformed values stay local. A field-only missing-identity query still
    # belongs to the normal read-only order review.
    return bool(
        VALUE_PATTERN.search(text)
        or re.search(r"(?:补充|填写|补上|录入)平台(?:订单号|单号)", text)
        or re.search(r"平台(?:订单号|单号)[^，,。；;！!？?\n]*[A-Za-z0-9]", text)
    )


def identity_intent_question(request: str) -> str | None:
    """Reject non-asserted identity data before constructing a confirmable plan.

    This is a conservative boundary for one field, not a general language
    classifier. Exact confirmation/cancellation is handled by the caller. A
    value inside an example, question or quoted instruction is not authority.
    Negation may exclude amount changes while explicitly requesting identity
    filling; other ambiguous scopes require a fresh, affirmative instruction.
    """
    text = re.sub(r"\s+", "", local_privacy_text(request))
    text = re.sub(r"ORD-[A-Za-z0-9-]+", "[系统订单]", text, flags=re.IGNORECASE)
    explanation = (
        "本次只解释，没有生成修改方案，也没有修改订单。补充平台订单号时，"
        "会先核对当前月份、平台订单归属、单号是否缺失及是否重复，再展示方案；"
        "只有确认该方案后才保存。需要实际补充时，请明确提供系统订单编号和真实平台订单号。"
    )
    # Quotes around a scalar value are supported by VALUE_PATTERN. Quotes
    # containing the field/instruction, including a whole pasted command, are
    # reported speech and cannot independently authorize a proposal.
    quoted = re.findall(
        r'“([^”]*)”|‘([^’]*)’|「([^」]*)」|『([^』]*)』|"([^"\n]*)"|\'([^\'\n]*)\'|`([^`]*)`',
        request,
    )
    if any(
        re.search(r"平台(?:订单号|单号)", part)
        for group in quoted for part in group if part
    ):
        return explanation
    if re.search(
        r"如果|假如|假设|假定|倘若|举例|例如|比如|示例|解释|说明一下|演示|讨论|咨询|"
        r"怎么|如何|会怎样|会怎么样|会发生什么|是否|能否|可以吗|对吗|[？?]",
        text,
    ):
        return explanation

    clauses = re.split(r"[，,。；;！!\n]|但是|但|而是", text)
    for clause in clauses:
        negative = re.search(r"不要|不|别|勿|无需|禁止", clause)
        if not negative:
            continue
        scope = clause[negative.end():]
        # Permit only a known, independently bounded exclusion. For example,
        # '不要改金额，只补平台单号' must retain its requested positive action.
        if re.fullmatch(r"(?:修改|更改|改|动|调整)(?:订单)?(?:金额|价格|房价)", scope) and re.search(r"(?:补充|补上|补|填写|填入|录入)平台(?:订单号|单号)", text):
            continue
        return explanation
    return None


def local_privacy_text(request):
    """Only this local-only typed field may contain an OTA/numeric identifier."""
    return VALUE_PATTERN.sub(
        lambda match: match[0].replace(match[1], "[平台单号]"), request
    )


def fingerprint(order, cycle):
    payload = [
        cycle.cycle_id,
        order.order_id,
        str(order.channel),
        order.platform_order_id,
        str(order.check_in_date),
        str(order.check_out_date),
        str(order.updated_at),
        str(order.actual_price),
        str(order.order_status),
        order.is_deleted,
    ]
    return sha256(json.dumps(payload, ensure_ascii=False).encode()).hexdigest()


async def answer_order_identity(
    db, cycle, actor_id, request, previous=None, parent_run_id=None, target_hint=None
):
    await _admin(db, actor_id)
    base = {"billing_month": cycle.billing_month, "request_text": request}

    def reply(state, message, **fields):
        return OrderIdentityFacts(**base, state=state, message=message, **fields)

    scope_text = re.sub(
        r"ORD-[A-Za-z0-9-]+",
        "[系统订单]",
        local_privacy_text(request),
        flags=re.IGNORECASE,
    )
    if question := month_scope_question(cycle.billing_month, scope_text, strict=True):
        return reply("clarification", question)
    command = re.sub(r"[\s，。！!]+", "", request)
    if command in CANCEL:
        return reply("cancelled", "这份补充平台订单号的方案已取消，没有修改订单。")
    confirming = command in CONFIRM
    if confirming and (not previous or previous.state not in {"proposal", "completed"}):
        return reply(
            "clarification",
            "请先提供需要补充的订单及真实平台订单号，查看方案后再确认。",
        )
    if confirming and previous.state == "completed":
        return reply(
            "completed",
            "这份方案之前已经执行，请继续查看最新订单核对结果。",
            order_id=previous.order_id,
        )
    if not confirming and (question := identity_intent_question(request)):
        return reply("clarification", question)
    if confirming:
        target, platform_id = previous.order_id, previous.platform_order_id
    else:
        ids = {
            value.upper()
            for value in re.findall(
                r"(?<![A-Za-z0-9-])ORD-[A-Za-z0-9-]+(?![A-Za-z0-9-])",
                request,
                flags=re.IGNORECASE,
            )
        }
        if not ids and target_hint:
            ids = {target_hint}
        values = VALUE_PATTERN.findall(request)
        if (
            len(ids) != 1
            or len(set(values)) != 1
            or not all(VALID_VALUE.fullmatch(value) for value in values)
        ):
            return reply(
                "clarification",
                "请发要补充的系统订单编号和真实平台订单号，例如“订单 ORD-… 的平台订单号是 ABC123”。我会先展示修改方案，确认后再保存。",
            )
        target, platform_id = next(iter(ids)), values[0]
    if not target or not platform_id or platform_id.upper().startswith("ORD-"):
        return reply(
            "clarification", "需要平台原始订单的真实单号，不能用系统 ORD 编号代替。"
        )
    if confirming:
        await acquire_month_financial_lock(db, cycle.billing_month)
        try:
            cycle = await require_cycle_writable(db, cycle)
        except MonthlyCloseConflict:
            return reply(
                "conflict", "本月已完成月结，请先重新打开月份，再生成新的修改方案。"
            )
        await _admin(db, actor_id, lock=True)
    order_query = (
        select(Order)
        .where(Order.order_id == target, Order.is_deleted.is_(False))
        .execution_options(populate_existing=True)
    )
    if confirming:
        order_query = order_query.with_for_update()
    order = await db.scalar(order_query)
    if (
        not order
        or not order.check_out_date
        or order.check_out_date.strftime("%Y-%m") != cycle.billing_month
        or order.channel not in OTA_PLATFORM_CHANNELS
    ):
        return reply(
            "conflict",
            "这笔订单不属于当前月份可补充单号的平台订单，请核对月份和系统订单编号。",
        )
    audit_key = f"monthly-close-order-identity:{parent_run_id}"
    if confirming:
        prior_audit = await db.scalar(
            select(AuditLog.log_id).where(
                AuditLog.action == "monthly_close.order_identity.fill",
                AuditLog.resource_id == target,
                AuditLog.operator_id == actor_id,
                AuditLog.notes == audit_key,
            )
        )
        if prior_audit and order.platform_order_id == platform_id:
            return reply(
                "completed",
                "这份方案之前已经保存，没有重复修改订单。",
                order_id=target,
                platform_order_id=platform_id,
            )
    if (order.platform_order_id or "").strip():
        return reply(
            "conflict",
            "这笔订单已经有平台订单号，本功能只补充缺失值，不覆盖已有单号。请重新查看订单核对结果。",
            order_id=target,
        )
    if cycle.status == "completed":
        return reply("conflict", "本月已完成月结，请先重新打开月份再补充订单资料。")
    if confirming and fingerprint(order, cycle) != previous.preview_hash:
        return reply(
            "conflict",
            "订单在方案生成后发生了变化，本次没有保存。请重新提供单号并核对新方案。",
            order_id=target,
        )
    try:
        await check_platform_identity_available(
            db, platform_id, target, lock=confirming
        )
    except ValueError:
        return reply(
            "conflict",
            "这个平台订单号已经关联其他订单，请先核对是否重复录入，本次没有保存。",
            order_id=target,
        )
    if not confirming:
        return reply(
            "proposal",
            f"将为订单 {target} 补充你提供的平台订单号。请核对下方单号，确认后回复“确认执行”。本次仅补充缺失单号。",
            order_id=target,
            platform_order_id=platform_id,
            preview_hash=fingerprint(order, cycle),
        )
    order.platform_order_id = platform_id
    await log_action_tx(
        db,
        actor_id,
        "monthly_close.order_identity.fill",
        "order",
        target,
        before_data={"platform_order_id": None},
        after_data={"platform_order_id": platform_id},
        notes=audit_key,
    )
    await db.flush()
    report = await order_integrity_snapshot(db, cycle)
    await db.commit()
    return reply(
        "completed",
        f"已补充订单 {target} 的平台订单号。重新检查后，订单完整性还剩 {report['blocking_count']} 项待处理。",
        order_id=target,
        platform_order_id=platform_id,
        remaining_count=report["blocking_count"],
    )
