"""Read-only explanation of the deployed cleaning settlement rules.

Rule sources: owner_settlement.compute_room_period_owner_stat, service_fees and
cleaning_request. This is policy explanation, never a per-expense verdict.
"""

import re


def asks_cleaning_responsibility(text: str) -> bool:
    value = re.sub(r"\s+", "", text)
    if re.search(r"改为|改成|修改|设置|记住|全部算|执行|补齐|删除|入账|转账", value):
        return False
    if re.search(r"水电|水费|电费|布草|洗涤|维修|平台|佣金|日耗|采购", value):
        return False
    return bool(
        re.search(r"保洁|打扫|清洁", value)
        and re.search(r"承担|谁.{0,4}(?:付|出)|谁的钱|谁买单|扣.{0,6}(?:业主|房东)|(?:业主|房东).{0,6}扣", value)
        and re.search(r"谁|吗|么|对吧|是不是|是否|为什么|怎么|规则|哪方|哪一方|[？?]", value)
        and not re.search(r"哪些|多少|明细|列出|\d{3,}", value)
    )


def cleaning_responsibility_message() -> str:
    return (
        "常规经营订单的正常打扫和续住打扫费用，按当前系统规则由业主承担 100%，在业主结算中扣除；但不能说所有情况都由业主承担。\n\n"
        "有两类例外：\n"
        "• 业主自用订单的打扫费用由公司承担，不计入业主结算。\n"
        "• 手工登记的保洁费用，如果明确标记为公司承担，也不会再扣给业主。\n\n"
        "这是当前结算规则说明，还没有逐笔确认本月每条打扫记录对应的费用和承担方。"
    )
