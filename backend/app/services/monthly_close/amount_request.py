"""Resolve bounded monetary follow-ups without borrowing a previous operation."""

import re

TOPICS = {
    "cleaning_statement": r"保洁|打扫|清洁",
    "linen_statement": r"布草|洗涤|洗衣",
    "utility_expense": r"水电|水费|电费|燃气",
    "operating_expenses": r"其他运营支出|日耗|耗材|采购|维修|宽带|物业费",
}

CATEGORY_WORDS = {
    "kitchen_cleaning": r"厨房保洁", "cleaning": r"保洁|打扫|清洁",
    "new_linen_prewash": r"过水|预洗", "laundry": r"洗涤|洗衣",
    "daily_supplies": r"日耗|耗材", "supplies": r"采购",
    "maintenance": r"维修", "electricity": r"电费",
    "water": r"水费", "gas": r"燃气", "broadband": r"宽带",
    "property_fee": r"物业费",
}


def unsupported_amount_scope(text):
    text = re.sub(r"\s+", "", text)
    return bool(re.search(r"收入|房费|利润|佣金|平台|OTA|携程|美团|(?:公司|业主|房东).{0,5}(?:承担|支付|出钱)|(?:承担|支付).{0,5}(?:公司|业主|房东)|房间|房号|号房|(?<!\d)\d{4}(?!\d)(?:的|房|号)|今天|昨天|\d{1,2}[日号]|20\d{2}-\d{2}-\d{2}", text, re.I))


def asks_amount(text):
    return bool(re.search(r"多少钱|金额|总额|合计|总共|一共|总支出|支出.*多少|费用.*多少", text))


def amount_request(question, history=(), explicit_context=None):
    if re.search(r"修改|改成|改为|删除|补齐|补录|执行|批准|确认|记住|恢复|入账|转账|格式|表头|怎么填|哪些列|谁承担|承担规则", question):
        return None
    if re.search(r"收入|房费|利润|佣金|平台|OTA|携程|美团|订单号|\d{3,}.*(?:房|保洁)", question, re.I):
        return None
    if unsupported_amount_scope(question):
        return None
    topics = [key for key, pattern in TOPICS.items() if re.search(pattern, question)]
    if len(topics) > 1:
        return None
    recent = [item for item in history if item.get("tool") != "conversation_memory"]
    if explicit_context:
        selected = next((i for i, item in enumerate(recent) if item.get("ref") == explicit_context), None)
        if selected is not None:
            recent = recent[:selected + 1]
    last = recent[-1] if recent else {}
    # A category supplied after "which costs?" completes the outstanding amount question.
    topic_only = re.fullmatch(r"(?:那|就|是)?(?:保洁|打扫|清洁|布草|洗涤|水电|电费|水费|运营支出)(?:的|费用)?[。！？?！\s]*", question)
    pending_amount = asks_amount(last.get("question", "")) or last.get("query_mode") == "amount_summary"
    if not asks_amount(question) and not (topic_only and pending_amount):
        return None
    focus = topics[0] if topics else None
    if focus is None and re.search(r"全部|支出|总额", question):
        focus = "all"
    if focus is None and re.search(r"这些|这个|那些|那一共|合计|一共", question):
        if last.get("tool") in {"cleaning_work_chat", "investigate_cleaning"}:
            focus = "cleaning_statement"
        elif last.get("tool") == "review_month" and last.get("focus") in {*TOPICS, "all"}:
            focus = last["focus"]
    if focus is None:
        return None
    categories = [key for key, pattern in CATEGORY_WORDS.items() if re.search(pattern, question)]
    if "kitchen_cleaning" in categories:
        categories.remove("cleaning")
    if "new_linen_prewash" in categories and "laundry" in categories:
        categories.remove("laundry")
    if not topics and focus != "all" and not categories:
        categories = last.get("expense_categories") or []
    result = {"tool": "review_month", "focus": focus, "mode": "amount_summary"}
    if categories:
        result["expense_categories"] = categories
    return result
