"""Local context selection and conservative read fallback. Never authorizes writes."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.services.monthly_close.cost_responsibility import asks_cleaning_responsibility
from app.services.monthly_close.amount_request import amount_request

TOPICS = {
    "cleaning_statement": r"保洁|打扫",
    "linen_statement": r"布草|洗涤|床单|被套|毛巾",
    "utility_expense": r"水电|电费|水费|充值",
    "ota_statement": r"OTA|携程|美团|飞猪|途家|平台账单",
    "operating_expenses": r"运营支出|采购|维修费|日耗|消耗品",
    "order_integrity": r"订单完整|订单这|订单那|订单.*(?:缺|问题|卡|处理)|平台(?:订单号|单号)",
    "exception_clearance": r"异常处理|异常事项",
    "preflight": r"结算前|预检",
    "settlement_review": r"业主结算|结算复核",
    "owner_confirmation": r"业主确认",
}
LABELS = {
    "all": "整月",
    "cleaning_statement": "保洁",
    "linen_statement": "布草洗涤",
    "utility_expense": "水电",
    "ota_statement": "平台账单",
    "operating_expenses": "运营支出",
    "order_integrity": "订单",
    "exception_clearance": "异常",
    "preflight": "结算前检查",
    "settlement_review": "业主结算",
    "owner_confirmation": "业主确认",
}


def topics_in(text):
    return [
        topic
        for topic, pattern in TOPICS.items()
        if re.search(pattern, text, re.IGNORECASE)
    ]


def read_fallback(question, history):
    """Only produce a read decision when the present request supplies safe scope."""
    if amount := amount_request(question, history):
        return amount
    if asks_cleaning_responsibility(question):
        return {
            "tool": "review_month",
            "focus": "cleaning_statement",
            "mode": "cost_responsibility",
        }
    if re.search(
        r"删除|删掉|补齐|补录|执行|批准|确认处理|恢复|撤销|改成|修改|关账|入账|转账|记住|忘记",
        question,
    ):
        return None
    if not re.search(
        r"查|看|问|哪些|什么|多少|怎么|为什么|格式|卡在|问题|进度|状态|还有|缺|接下来|下一步|然后|呢",
        question,
    ):
        return None
    # Earlier acknowledged topics must not override an explicit new focus.
    scoped = re.split(
        r"(?:先告诉我|先看|先查|先说|换到|换成|现在查|现在看|接下来查)", question
    )[-1]
    topics = topics_in(scoped)
    compact = re.sub(r"[\s，,。.!！?？：:]+", "", scoped)
    broad = re.search(
        r"月结|整月|整个月|本月.*(?:待办|问题|进度|状态)|账单.*(?:没.*对|待|问题|进度)",
        scoped,
    )
    focus = (
        topics[0] if len(topics) == 1 else "all" if broad or len(topics) > 1 else None
    )
    # Inherit only a short referential follow-up, never a new unrelated subject.
    relative = re.fullmatch(
        r"为什么(?:呢|会这样)?|这是什么原因|(?:那|这|那这)?(?:需要什么格式|要什么格式|怎么填|怎么处理|一步怎么做)|这些列怎么填|然后呢|下一步(?:呢|是什么)?|接下来(?:呢|做什么)?|还有什么(?:没对|没核对|待处理)",
        compact,
    )
    if focus is None and relative:
        for item in reversed(history):
            if item.get("tool") == "conversation_memory":
                continue
            if item.get("tool") == "review_month" and item.get("focus") in LABELS:
                focus = item["focus"]
            break
    if focus is None:
        return None
    mode = (
        "format"
        if re.search(r"格式|怎么填|哪些列|表头", scoped)
        else "next_step"
        if re.search(r"下一步|接下来|然后", scoped)
        else "explain"
        if re.search(r"为什么|怎么|卡|问题", scoped)
        else "status"
    )
    return {"tool": "review_month", "focus": focus, "mode": mode}


class MemoryNote(BaseModel):
    model_config = ConfigDict(extra="forbid")
    topic: str
    text: str = Field(max_length=1000)


class ConversationMemoryFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    kind: Literal["conversation_memory"] = "conversation_memory"
    billing_month: str
    request_text: str
    state: Literal["saved", "cleared", "report", "clarification"]
    notes: list[MemoryNote] = Field(default_factory=list, max_length=12)
    message: str
    integrity_valid: bool = True


def memory_request(question):
    if re.search(
        r"^(?:请)?(?:查看|列出|看看|还记得)(?:本月|这个月|我说过的)?(?:记忆|要求|处理原则)",
        question,
    ):
        return "report", ""
    match = re.match(
        r"^(?:请)?(?:本月|这个月)?(?:请)?(?:记住|记一下|记下)(?:本月|这个月)?[：:,，\s]*(.+)$",
        question,
        re.DOTALL,
    )
    if match:
        return "save", match[1].strip()
    match = re.match(
        r"^(?:请)?(?:忘记|清除|取消记住)(?:本月|这个月)?[：:,，\s]*(.*)$",
        question,
        re.DOTALL,
    )
    if match:
        return "clear", match[1].strip()
    return None


def memory_answer(month, request, command, existing):
    operation, instruction = command
    from app.services.monthly_close.semantic_agent import month_scope_question

    notes = list(existing or [])
    if operation in {"save", "clear"} and (
        scope_question := month_scope_question(month, instruction, strict=True)
    ):
        return ConversationMemoryFacts(
            billing_month=month,
            request_text=request,
            state="clarification",
            notes=notes,
            integrity_valid=existing is not None,
            message=scope_question,
        )
    if existing is None and not (
        operation == "clear"
        and re.fullmatch(r"(?:全部|所有)(?:记忆|要求|处理原则|原则)?", instruction)
    ):
        return ConversationMemoryFacts(
            billing_month=month,
            request_text=request,
            state="clarification",
            notes=[],
            integrity_valid=False,
            message="已保存的处理原则未通过完整性检查，暂时不会用于回答。请说“清除本月全部记忆”来重置，再重新保存要求；原始聊天和业务数据会保留。",
        )
    state = "report"
    if operation == "save":
        from app.services.monthly_close.order_identity_chat import VALUE_PATTERN

        if VALUE_PATTERN.search(instruction) or re.search(
            r"ORD-[A-Za-z0-9-]+", instruction, re.IGNORECASE
        ):
            return ConversationMemoryFacts(
                billing_month=month,
                request_text=request,
                state="clarification",
                notes=notes,
                message="具体订单编号和平台单号请放在对应业务记录中。本月记忆只保存处理原则；这次没有保存单号或修改订单。",
            )
        if len(instruction) > 1000:
            return ConversationMemoryFacts(
                billing_month=month,
                request_text=request,
                state="clarification",
                notes=notes,
                message="这条处理原则较长，请拆成每条不超过 1000 字的要求再保存。现有记忆没有改变。",
            )
        topic = topics_in(instruction)
        key = topic[0] if len(topic) == 1 else "all"
        if any(note.text == instruction for note in notes):
            pass
        elif len(notes) >= 12:
            return ConversationMemoryFacts(
                billing_month=month,
                request_text=request,
                state="clarification",
                notes=notes,
                message="本月已保存 12 条处理原则。请先查看并清除不再适用的要求，再保存新要求；我没有悄悄丢弃旧要求。",
            )
        else:
            notes.append(MemoryNote(topic=key, text=instruction))
        state = "saved"
        message = f"已记住你在 {month} 的这条{LABELS[key]}处理原则；已有原则也会保留；有冲突时以你这次的明确要求为准。后续查询和方案会带上它，具体修改仍需核对当前记录后确认。"
    elif operation == "clear":
        selected = topics_in(instruction)
        if selected:
            notes = [
                note
                for note in notes
                if note.topic not in selected
                and not set(topics_in(note.text)).intersection(selected)
            ]
        elif not instruction or re.search(r"全部|所有|记忆|要求|原则", instruction):
            notes.clear()
        else:
            return ConversationMemoryFacts(
                billing_month=month,
                request_text=request,
                state="clarification",
                notes=notes,
                message="请说明要忘记哪类处理原则，或说“清除本月全部记忆”。现有记忆没有改变。",
            )
        state, message = (
            "cleared",
            "已清除包含指定主题的整条本月处理原则；原始聊天记录和业务数据没有删除。"
            if selected
            else "已清除本月处理原则；原始聊天记录和业务数据没有删除。",
        )
    else:
        message = (
            "这是你在本月保存的处理原则。可以说“记住：……”来更新，或“忘记保洁的要求”来清除某一类。"
            if notes
            else "本月还没有保存的处理原则。可以说“记住：保洁按原表核对”，后续无需反复说明。"
        )
    return ConversationMemoryFacts(
        billing_month=month,
        request_text=request,
        state=state,
        notes=notes,
        message=message,
    )


def explanation_only(question):
    return bool(
        re.search(
            r"(?:只|先|仅)(?:需要|想|要|帮我|给我)?(?:解释|说明|分析)|(?:不要|别|先别|不需要|不用|禁止).{0,8}(?:生成|创建|准备|制定|给我).{0,4}方案|只是?(?:举例|假设)|举个例|(?:如果|假如|假设).{0,150}(?:会|怎么|如何)",
            question,
            re.DOTALL,
        )
    )
