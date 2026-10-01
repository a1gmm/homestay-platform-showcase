"""Grounded OCR expense candidates, not accounting entries or payment proof.

Only visible numeric tokens are parsed. Totals and detail rows describe alternate
levels of the same evidence; no automatic net allocation or ownership inference
is made. Every candidate requires an explicit review in the calling workflow.
"""
from __future__ import annotations

import re
from collections import defaultdict
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_FLOOR
from hashlib import sha256

NUMBER = r"[+-]?\d[\d,，]*(?:[.．]\s*\d{1,2})?"
MONEY = re.compile(r"(?<![\d.])(" + NUMBER + r")(?![\d.])")
ROOM_ROW = re.compile(r"^\s*([A-Za-z0-9]{1,8}(?:[-－]\d{1,5}){1,5}|\d{4})\s+(.+)$")
CUES = {
    "property": r"物业(?:服务)?费|物业服务",
    "water": r"水费|水表充值",
    "electricity": r"电费|电表充值",
    "maintenance": r"维修|维保|修理|保养|检修",
    "cleaning_supplier": r"保洁|清洁服务|清扫费",
    "laundry_supplier": r"洗涤|洗衣|布草清洗",
    "supplies": r"耗材|日用品|采购|办公用品",
    "payroll": r"工资|薪酬|薪资",
    "social_insurance": r"社保|公积金|五险",
    "tax": r"税费|税款|缴税",
    "bank_fee": r"银行手续费|账户管理费",
}
COUNT = {"一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
         "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}


def _decimal(text):
    try:
        value = Decimal(re.sub(r"\s", "", str(text)).replace(",", "").replace("，", "").replace("．", "."))
        return value if value.is_finite() and abs(value) < Decimal("1000000000") else None
    except InvalidOperation:
        return None


def _money(value):
    return format(value.quantize(Decimal("0.01")), "f") if value is not None else None


def _numbers(text):
    return [value for match in MONEY.finditer(text) if (value := _decimal(match[1])) is not None]


def _category(text):
    values = [category for category, cue in CUES.items() if re.search(cue, text)]
    return values[0] if len(values) == 1 else "unknown"


def _date(text):
    dates = []
    for match in re.finditer(r"(20\d{2})[-/.年](\d{1,2})[-/.月](\d{1,2})(?:日|\b)", text):
        try:
            dates.append(date(*map(int, match.groups())).isoformat())
        except ValueError:
            pass
    unique = set(dates)
    return next(iter(unique)) if len(unique) == 1 else None


def _billing_month(text):
    # A screenshot/payment/message date is not an expense-period declaration.
    patterns = [r"(?:费用所属月份|费用月份|账期|所属月份|业务月份)\s*[:：]?\s*(20\d{2})[年/.-](\d{1,2})(?:月)?",
                r"(20\d{2})年(\d{1,2})月(?:份)?(?:的)?(?:物业|水费|电费|维修|维保|保洁|洗涤|工资|社保|税费)"]
    periods = {f"{year}-{int(month):02d}" for pattern in patterns for year, month in re.findall(pattern, text) if 1 <= int(month) <= 12}
    return next(iter(periods)) if len(periods) == 1 else None


def _room(text):
    match = re.search(r"(?:房号|房间)\s*[:：]?\s*([A-Za-z0-9-]{3,40})|(?<!\d)(\d{4})\s*(?:号房|房)", text)
    return next((v for v in match.groups() if v), None) if match else None


def _ref(row, text, role):
    return dict(sheet="图片", row1based=row, role=role, text=text[:800])


def _candidate(digest, row, amount, category, text, *, basis, day=None, room=None, refs=None, **fields):
    key = sha256(f"{digest}:image-expense:{row}:{basis}:{category}:{_money(amount)}".encode()).hexdigest()
    return dict(key=key, kind="image_expense_candidate", sheet="图片", row1based=row,
        category=category, amount=_money(amount), room_ref=room, date=day,
        business_month=None, payer="unconfirmed", paid_by="unconfirmed",
        description="图片费用候选，待核实", original_text=text[:800],
        evidence_refs=refs or [_ref(row, text, basis)],
        fields=dict(basis=basis, countable=False, requires_confirmation=True,
            date_basis="visible_date_needs_confirmation" if day else "unknown",
            provider_collection_perspective="unconfirmed", **fields))


def _proportional_net(facts, net):
    gross = sum((Decimal(f["amount"]) for f in facts), Decimal(0))
    if gross <= 0 or net < 0 or net > gross or any(Decimal(f["amount"]) < 0 for f in facts):
        return None
    cents = int(net * 100)
    quotas = [Decimal(cents) * Decimal(f["amount"]) / gross for f in facts]
    floors = [int(q.to_integral_value(rounding=ROUND_FLOOR)) for q in quotas]
    remainder = cents - sum(floors)
    order = sorted(range(len(facts)), key=lambda i: (-(quotas[i] - floors[i]), facts[i]["row1based"], facts[i]["key"]))
    for index in order[:remainder]:
        floors[index] += 1
    return [_money(Decimal(value) / 100) for value in floors]


def effective_image_fact(fact: dict, decision: dict) -> dict:
    """Apply only the explicitly reviewed proportional allocation to a copy.

    Raw OCR evidence and original amount remain unchanged. This helper grants no
    posting authority; caller still checks room, period, category and both parties.
    """
    result = deepcopy(fact)
    fields = result.setdefault("fields", {})
    derived = fields.get("discounted_amount_proportional")
    if (fact.get("kind") == "image_expense_candidate" and decision.get("status") == "confirmed"
        and decision.get("candidate_confirmed") is True and decision.get("discount_allocation") == "proportional"
        and derived is not None and _decimal(derived) is not None):
        fields["original_amount"] = fact.get("amount")
        fields["applied_discount_allocation"] = "proportional"
        result["amount"] = derived
    return result


def extract_image_expense_evidence(text: str, image_sha: str) -> dict:
    """Return facts/summaries/issues for actual OCR text without inferring amounts."""
    result = dict(facts=[], summaries=[], issues=[])
    lines = list(enumerate(text.splitlines(), 1))
    nonempty = [(n, line.strip()) for n, line in lines if line.strip()]
    global_category = _category(text)
    global_date = _date(text)
    global_month = _billing_month(text)
    table_header = next(((n, line) for n, line in nonempty
        if re.search(r"房间|房号", line) and "金额" in line), None)
    consumed = set()
    detail_facts = []
    # A room table needs an amount-labelled header. Phone lists and bank account
    # text can therefore never be mistaken for per-room fees.
    if table_header:
        for n, line in nonempty:
            if n <= table_header[0]:
                continue
            match = ROOM_ROW.match(line)
            if not match:
                continue
            values = _numbers(match[2])
            if not values or ("面积" in table_header[1] and len(values) < 2):
                result["issues"].append(dict(code="image_room_amount_unreadable", message="图片房间行缺少可识别的独立费用金额，未把面积当作金额", sheet="图片", row1based=n, severity="warning"))
                continue
            amount_tail = re.search(r"(" + NUMBER + r")\s*(?:元|[|丨])?\s*$", match[2])
            if not amount_tail or re.search(r"\d\s+$", match[2][:amount_tail.start()]):
                result["issues"].append(dict(code="image_room_amount_unreadable", message="图片房间行末的金额无法确认，未生成该行候选", sheet="图片", row1based=n, severity="warning"))
                continue
            amount = values[-1]
            category = _category(line)
            if category == "unknown":
                category = global_category
            fact = _candidate(image_sha, n, amount, category, line, basis="room_detail",
                day=_date(line) or global_date, room=match[1],
                refs=[_ref(table_header[0], table_header[1], "table_header"), _ref(n, line, "room_amount")],
                selection_group=f"{image_sha}:table", amount_basis="provider_room_charge")
            detail_facts.append(fact)
            consumed.add(n)
        result["facts"].extend(detail_facts)
    detail_total = sum((Decimal(f["amount"]) for f in detail_facts), Decimal(0))
    reported = discount = net = None
    tail_start = max((f["row1based"] for f in detail_facts), default=0)
    for n, line in nonempty:
        if n in consumed:
            continue
        values = _numbers(line)
        if not values:
            continue
        amount = values[-1]
        role = None
        if re.search(r"优惠|折扣|减免", line):
            role = "discount"
        elif re.search(r"实缴|实付|实收|已收|已付|到账|实际支付", line):
            role = "provider_net"
        elif re.search(r"合计|总计|总额|应收|应付", line):
            role = "gross"
        elif detail_facts and n > tail_start and amount == detail_total and len(values) == 1:
            # A garbled total label can be identified only by an exact independent
            # numeric match to all visible detail rows, after the last detail.
            role = "gross"
        if not role:
            continue
        category = _category(line)
        if category == "unknown":
            categories = {f["category"] for f in detail_facts}
            category = next(iter(categories)) if len(categories) == 1 else global_category
        summary = dict(kind="image_expense_summary", role=role, category=category,
            amount=_money(amount), sheet="图片", row1based=n,
            business_month=global_month,
            countable=False, requires_confirmation=True, evidence_refs=[_ref(n, line, role)],
            source_date=global_date, selection_group=f"{image_sha}:table")
        result["summaries"].append(summary)
        consumed.add(n)
        if role == "gross":
            reported = summary
        elif role == "discount":
            discount = summary
        else:
            net = summary
    if detail_facts:
        result["summaries"].append(dict(kind="image_detail_total", amount=_money(detail_total),
            category=detail_facts[0]["category"] if len({f["category"] for f in detail_facts}) == 1 else "unknown",
            count=len(detail_facts), room_refs=[f["room_ref"] for f in detail_facts],
            countable=False, selection_group=f"{image_sha}:table"))
        if reported and Decimal(reported["amount"]) != detail_total:
            result["issues"].append(dict(code="image_detail_total_mismatch", message="图片房间明细与汇总金额不符，不能直接生成费用", severity="error"))
    if reported and discount and net:
        matches = Decimal(reported["amount"]) - Decimal(discount["amount"]) == Decimal(net["amount"])
        result["summaries"].append(dict(kind="image_discount_reconciliation", gross=reported["amount"],
            discount=discount["amount"], net=net["amount"], matches=matches, countable=False))
        if not matches:
            result["issues"].append(dict(code="image_discount_equation_mismatch", message="图片合计减优惠与实缴金额不符，需回看原图", severity="error"))
    chosen_summary = net or reported
    if chosen_summary:
        n = chosen_summary["row1based"]
        line = dict(lines)[n]
        fact = _candidate(image_sha, n, Decimal(chosen_summary["amount"]), chosen_summary["category"], line,
            basis="summary", day=global_date, refs=chosen_summary["evidence_refs"],
            summary_role=chosen_summary["role"], selection_group=f"{image_sha}:table",
            room_refs=[f["room_ref"] for f in detail_facts], allocation_required=bool(detail_facts),
            mutually_exclusive_with=[f["key"] for f in detail_facts],
            amount_basis="provider_received_total" if net else "provider_charge_total")
        result["facts"].append(fact)
        for detail in detail_facts:
            detail["fields"]["mutually_exclusive_with"] = [fact["key"]]
            if discount and Decimal(discount["amount"]) != 0:
                detail["fields"]["discount_allocation_required"] = True
                detail["fields"]["unallocated_discount"] = discount["amount"]
        if (detail_facts and reported and discount and net
            and Decimal(reported["amount"]) == detail_total
            and Decimal(reported["amount"]) - Decimal(discount["amount"]) == Decimal(net["amount"])):
            amounts = _proportional_net(detail_facts, Decimal(net["amount"]))
            if amounts is not None:
                for detail, amount in zip(detail_facts, amounts):
                    detail["fields"]["discounted_amount_proportional"] = amount
                    detail["fields"]["discount_allocation_method"] = "proportional_largest_remainder_cents"
                result["summaries"].append(dict(kind="image_proportional_allocation_preview",
                    gross=_money(detail_total), net=net["amount"], discount=discount["amount"],
                    allocated_total=_money(sum((Decimal(a) for a in amounts), Decimal(0))),
                    requires_explicit_decision="discount_allocation=proportional", countable=False))
        if detail_facts and discount and Decimal(discount["amount"]) != 0:
            result["issues"].append(dict(code="image_discount_allocation_required", message="优惠仅在图片总额体现，不能擅自分摊到各房间；明细与实缴汇总不可重复计费", severity="warning"))
    # Chat quotations: infer multiplication only from an explicit count + each
    # price and a visible fee cue nearby in the same image.
    unit_pattern = re.compile(r"([一二两三四五六七八九十]|\d{1,3})\s*(层|楼|间|个|次|天|套|台)\s*(?:各|每(?:层|楼|间|个|次|天|套|台))\s*(" + NUMBER + r")")
    unit_quotes = set()
    for position, (n, line) in enumerate(nonempty):
        match = unit_pattern.search(line)
        if not match or n in consumed:
            continue
        count = COUNT.get(match[1]) or (int(match[1]) if match[1].isdigit() else None)
        price = _decimal(match[3])
        if not count or count > 100 or price is None or price <= 0:
            continue
        nearby = [(row, value) for row, value in nonempty[max(0, position - 6):position + 1]
                  if _category(value) != "unknown"]
        if not nearby:
            continue
        cue_row, cue_text = nearby[-1]
        category = _category(cue_text)
        refs = [_ref(cue_row, cue_text, "fee_purpose"), _ref(n, line, "quantity_and_unit_price")]
        result["facts"].append(_candidate(image_sha, n, price * count, category, line,
            basis="unit_count", day=_date(line) or global_date, room=_room(line), refs=refs,
            quantity=count, quantity_unit=match[2], unit_amount=_money(price),
            floor_or_room_assignment="unconfirmed", amount_basis="quoted_expense"))
        consumed.add(n)
        unit_quotes.add((category, price))
    for position, (n, line) in enumerate(nonempty):
        if n in consumed or (table_header and n >= table_header[0]):
            continue
        # Currency or 元 is mandatory outside a structured amount column. This
        # avoids treating phone numbers, dates, clock times and room IDs as money.
        matches = list(re.finditer(r"(?:[¥￥]\s*(" + NUMBER + r")|(" + NUMBER + r")\s*(?:元|块钱))", line))
        explicit_fee_amount = None
        if not matches and _category(line) != "unknown":
            match = re.search(r"(?:费|工资|薪资|税款)\s*[:：]\s*(" + NUMBER + r")\s*$", line)
            if match:
                explicit_fee_amount = _decimal(match[1])
        if len(matches) != 1 and explicit_fee_amount is None:
            continue
        if matches and re.search(r"\d\s+$", line[:matches[0].start()]):
            result["issues"].append(dict(code="image_amount_token_ambiguous", message="图片金额被空格拆开，不能将尾部数字作为完整金额", sheet="图片", row1based=n, severity="warning"))
            continue
        amount = explicit_fee_amount if explicit_fee_amount is not None else _decimal(matches[0][1] or matches[0][2])
        nearby = [(row, value) for row, value in nonempty[max(0, position - 3):position + 1]
                  if _category(value) != "unknown"]
        if amount is None or amount <= 0 or not nearby:
            continue
        cue_row, cue_text = nearby[-1]
        category = _category(cue_text)
        if (category, amount) in unit_quotes or re.search(r"每(?:层|间|个|次|天)|单价", line):
            continue
        result["facts"].append(_candidate(image_sha, n, amount, category, line,
            basis="explicit_amount", day=_date(line) or global_date, room=_room(line),
            refs=[_ref(cue_row, cue_text, "fee_purpose"), _ref(n, line, "currency_amount")],
            amount_basis="visible_fee_amount"))
    if not result["facts"]:
        result["issues"].append(dict(code="image_no_grounded_expense", message="未发现同时具有费用依据和可靠金额的图片明细，不能把聊天数字或截图时间当作费用", severity="warning"))
    else:
        result["issues"].append(dict(code="image_candidates_unconfirmed", message="图片金额仅为待确认候选；费用类别、房间、日期、月份、承担方、实际付款方及是否重复均须核实", severity="warning"))
        if any(f["category"] == "unknown" for f in result["facts"]):
            result["issues"].append(dict(code="image_expense_category_unknown", message="部分图片费用科目未可靠识别，不能根据房间或金额猜测类别", severity="warning"))
    for fact in result["facts"]:
        fact["business_month"] = global_month
    return result


def compare_image_expense_sources(sources: list[dict]) -> list[dict]:
    """Compare independent image controls with workbook expense groups.

    Missing image business period is explicitly a possible conflict, never proof
    that the workbook and image refer to the same liability or payment.
    """
    workbooks = defaultdict(lambda: Decimal(0))
    controls = []
    canonical_category = lambda value: {"property_fee": "property"}.get(value, value)
    for source in sources:
        source_id = source.get("source_id")
        parsed = source.get("parsed") or {}
        decisions = source.get("decisions") or {}
        for fact in parsed.get("facts", []):
            if fact.get("kind") == "source_expense":
                decision = decisions.get(fact.get("key")) or {}
                confirmed = decision.get("status") == "confirmed"
                if confirmed and decision.get("include") is False:
                    continue
                amount = _decimal(fact.get("amount"))
                if amount is not None:
                    category = decision.get("category", fact.get("category")) if confirmed else fact.get("category")
                    month = decision.get("business_month", fact.get("business_month")) if confirmed else fact.get("business_month")
                    workbooks[(source_id, canonical_category(category), month)] += amount
        summaries = [s for s in parsed.get("summaries", []) if s.get("kind") == "image_expense_summary"]
        for group in {s.get("selection_group") for s in summaries}:
            candidates = [s for s in summaries if s.get("selection_group") == group]
            chosen = next((s for s in candidates if s.get("role") == "provider_net"), None) or next((s for s in candidates if s.get("role") == "gross"), None)
            image_facts = [f for f in parsed.get("facts", []) if f.get("kind") == "image_expense_candidate" and f.get("fields", {}).get("selection_group") == group]
            selected = [f for f in image_facts if not ((decisions.get(f.get("key")) or {}).get("status") == "confirmed" and (decisions.get(f.get("key")) or {}).get("include") is False)]
            if image_facts and not selected:
                continue
            if chosen:
                chosen = dict(chosen)
                if selected and all((decisions.get(f.get("key")) or {}).get("status") == "confirmed" for f in selected):
                    periods = {(decisions.get(f["key"]) or {}).get("business_month", f.get("business_month")) for f in selected}
                    categories = {canonical_category((decisions.get(f["key"]) or {}).get("category", f.get("category"))) for f in selected}
                    if len(periods) == 1 and None not in periods:
                        chosen["business_month"] = next(iter(periods))
                    if len(categories) == 1:
                        chosen["category"] = next(iter(categories))
                chosen["category"] = canonical_category(chosen.get("category"))
                if chosen.get("category") not in {"unknown", None}:
                    controls.append((source_id, chosen))
    issues = []
    for image_source, control in controls:
        for (source_id, category, month), observed in workbooks.items():
            if source_id == image_source or category != control.get("category"):
                continue
            image_month = control.get("business_month")
            if image_month and month and image_month != month:
                continue
            expected = Decimal(control["amount"])
            if expected == observed:
                continue
            issues.append(dict(code="possible_image_expense_total_conflict", severity="warning",
                message="图片汇总与同类费用表金额不同；需先核实是否同一期间、同组房间及同一费用，不能直接补记差额",
                source_id=image_source, other_source_id=source_id, category=category,
                image_total=_money(expected), workbook_total=_money(observed), difference=_money(expected - observed),
                image_business_month=image_month, workbook_business_month=month,
                period_status="same_month_claimed" if image_month and month else "unknown",
                identity_status="unconfirmed", countable=False))
    return issues
