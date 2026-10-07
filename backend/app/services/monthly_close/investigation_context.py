"""Resolve read follow-ups against verified, actor-local evidence references.

This module selects a read scope, never copies an answer or grants write authority.
The same resolver works with the language model disabled or unavailable.
"""
from __future__ import annotations

import re

from app.services.monthly_close.conversation_context import topics_in


def _number(value):
    if value.isdigit():
        return int(value)
    digits = {char: index for index, char in enumerate("零一二三四五六七八九")}
    value = value.replace("两", "二")
    if value == "一百":
        return 100
    if "十" in value:
        tens, ones = value.split("十", 1)
        if (not tens or tens in digits) and (not ones or ones in digits):
            return digits.get(tens, 1) * 10 + digits.get(ones, 0)
    return digits.get(value)


def read_reference(text: str, context: dict) -> dict | None:
    # Explicit read-only negations are constraints, not mutation requests.
    query = re.sub(r"(?:不要|不用|不必|禁止|不能|别|不)(?:生成|执行|进行|做|任何|或|和|及|账务|修改|操作|新|方案|记账|写入|删除|确认|批准|\s)+", "", text)
    if re.search(r"删除|删掉|移除|撤掉|撤下|补齐|补录|补上|执行|批准|确认处理|恢复|还原|撤销|改成|改为|修改|调整|关账|入账|转账|记住|忘记", query):
        return None
    ordinal = re.search(r"第(\d+|[零一二两三四五六七八九十百]+)(?:[项笔条]|个(?!(?:的)?(?:文件|资料|附件|方案|月|步骤))|的那个|那个)", query)
    detailed = ordinal or re.search(r"具体|逐项|逐笔|明细|分别|展开|哪几项|哪几笔|[这那].{0,5}[项笔条]", query)
    referential = ordinal or re.search(r"这些|那[些几]|[这那][几\d一二两三四五六七八九十百]+[项笔条]|刚才|上面|前面|继续|逐项|逐笔|展开", query)
    explicit_topics = topics_in(query)
    if not detailed or (not referential and len(explicit_topics) != 1):
        return None
    history = context.get("history", [])
    selected_ref = context.get("explicit_context")
    prior = next((item for item in history if item.get("ref") == selected_ref), None) if selected_ref else next(
        (item for item in reversed(history) if item.get("tool") != "conversation_memory"), None)
    if not prior:
        return None
    scopes = prior.get("investigation_scopes") or []
    count = re.search(r"(?:这|那)(\d+|[零一二两三四五六七八九十百]+)[项笔条]", query)
    candidates = [scope for scope in scopes if scope.get("count", 0) > 0]
    if count:
        candidates = [scope for scope in candidates if scope["count"] == _number(count[1])]
    if len(explicit_topics) == 1:
        candidates = [scope for scope in scopes if scope.get("topic") == explicit_topics[0]]
        if not candidates:
            return {"tool": "review_month", "focus": explicit_topics[0], "mode": "details"}
    elif len(explicit_topics) > 1:
        return None
    elif prior.get("query_mode") == "details" and prior.get("focus") not in {None, "all"}:
        candidates = [scope for scope in candidates if scope.get("topic") == prior["focus"]]
    if len(candidates) != 1:
        if len(candidates) > 1:
            labels = "、".join(dict.fromkeys(scope.get("label", scope["topic"]) for scope in candidates))
            return {"tool": "clarify", "question": f"刚才有多组事项（{labels[:90]}），你要展开哪一组？可以直接说类别或文件名。"}
        if count:
            return {"tool": "clarify", "question": "刚才的结果中没有唯一对应这个数量的事项组。请告诉我是保洁、订单还是另一类差异，我会重新读取当前清单。"}
        return None
    chosen = candidates[0]
    result = {"tool": "review_month", "focus": chosen["topic"], "mode": "details", "context_ref": prior["ref"]}
    if chosen.get("document_ref"):
        result["document_ref"] = chosen["document_ref"]
    if ordinal:
        number = _number(ordinal[1])
        if number is None or not 1 <= number <= 100:
            return {"tool": "clarify", "question": "请提供清单中展示的序号，或直接说明日期与房间。"}
        result["case_ordinal"] = number
        # A total in an overview is not an ordered list. Do not guess its second row.
        if prior.get("query_mode") != "details" or not prior.get("detail_evidence_hash"):
            return {"tool": "clarify", "question": "上一条还没有列出完整的逐项顺序。请先让我展开这类差异，再按序号选择；也可以直接提供日期和房间。"}
        if prior.get("visible_ordinals") is not None and result["case_ordinal"] not in prior["visible_ordinals"]:
            return {"tool": "clarify", "question": "你选择的序号不在上一条展示的范围内。请提供日期、房间，或先展开对应清单；我不会套用另一组记录的序号。"}
    return result
