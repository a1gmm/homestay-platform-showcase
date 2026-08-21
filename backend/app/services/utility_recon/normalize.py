"""把异构表格行转换为确定性水电对账契约。"""

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
import re

from .contracts import DetectedTable, NormalizationResult, NormalizedRow


@dataclass(frozen=True)
class NormalizedValue:
    value: object | None
    warning: str | None = None
    excluded: bool = False


_CN_NUMBERS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}


def _chinese_integer(text: str) -> int | None:
    text = text.strip()
    if text == "十":
        return 10
    if "十" in text:
        left, right = text.split("十", 1)
        tens = _CN_NUMBERS.get(left, 1 if not left else 0)
        ones = _CN_NUMBERS.get(right, 0 if not right else -100)
        value = tens * 10 + ones
        return value if value >= 0 else None
    return _CN_NUMBERS.get(text)


def normalize_floor(raw: object) -> NormalizedValue:
    text = re.sub(r"\s+", "", str(raw or ""))
    numeric = re.search(r"(?<!\d)(-?\d{1,3})(?:层|楼|f)?", text, re.IGNORECASE)
    if numeric:
        return NormalizedValue(f"{int(numeric.group(1))}层")
    chinese = re.search(r"([一二三四五六七八九十]{1,3})(?:层|楼)", text)
    if chinese and (value := _chinese_integer(chinese.group(1))) is not None:
        return NormalizedValue(f"{value}层")
    return NormalizedValue(None, "楼层无法解析")


def normalize_room(raw: object) -> NormalizedValue:
    text = re.sub(r"[\s#号室]", "", str(raw or ""))
    return NormalizedValue(text or None, None if text else "房间号缺失")


def normalize_category(raw: object) -> NormalizedValue:
    text = re.sub(r"\s+", "", str(raw or "")).lower()
    non_utility = ("维修", "服务费", "押金", "保洁", "清洁", "物业费")
    if any(label in text for label in non_utility):
        return NormalizedValue(None, "非水电费项目", True)
    has_water = "水费" in text or text == "水"
    has_electricity = "电费" in text or text == "电"
    if has_water and not has_electricity:
        return NormalizedValue("water")
    if has_electricity and not has_water:
        return NormalizedValue("electricity")
    return NormalizedValue(None, "科目无法解析")


def normalize_amount(raw: object) -> NormalizedValue:
    if raw is None or isinstance(raw, bool):
        return NormalizedValue(None, "金额无法解析")
    text = str(raw).strip().replace(",", "").replace("¥", "").replace("￥", "")
    negative = text.startswith("(") and text.endswith(")")
    if negative:
        text = text[1:-1]
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return NormalizedValue(None, "金额无法解析")
    if negative:
        value = -value
    return NormalizedValue(value.quantize(Decimal("0.01")))


def normalize_date(raw: object) -> NormalizedValue:
    if isinstance(raw, datetime):
        return NormalizedValue(raw.date())
    if isinstance(raw, date):
        return NormalizedValue(raw)
    text = str(raw or "").strip()
    match = re.search(r"(20\d{2})\D{0,3}(1[0-2]|0?[1-9])\D{0,3}(3[01]|[12]\d|0?[1-9])", text)
    if not match:
        return NormalizedValue(None, "日期无法解析")
    try:
        return NormalizedValue(date(*(int(part) for part in match.groups())))
    except ValueError:
        return NormalizedValue(None, "日期无法解析")


def _cell(row: tuple, columns: dict[str, int], key: str) -> object:
    index = columns.get(key)
    return row[index] if index is not None and index < len(row) else None


def normalize_table(table: DetectedTable) -> NormalizationResult:
    buckets: dict[str, list[NormalizedRow]] = {"valid": [], "excluded": [], "unparseable": []}
    for offset, source in enumerate(table.rows, start=table.header_row + 1):
        raw_date = _cell(source, table.columns, "date")
        raw_summary = _cell(source, table.columns, "summary")
        raw_floor = _cell(source, table.columns, "floor") or raw_summary
        raw_category = _cell(source, table.columns, "category") or raw_summary
        amount_key = "receipt_amount" if table.role == "receipt" else "expense_amount"
        raw_amount = _cell(source, table.columns, amount_key)
        parsed_date = normalize_date(raw_date)
        floor = normalize_floor(raw_floor)
        category = normalize_category(raw_category)
        amount = normalize_amount(raw_amount)
        room = normalize_room(_cell(source, table.columns, "room"))
        warnings = tuple(item for item in (parsed_date.warning, floor.warning, category.warning, amount.warning) if item)
        if category.excluded:
            disposition = "excluded"
            reason = category.warning
        elif any(value.value is None for value in (parsed_date, floor, category, amount)):
            disposition = "unparseable"
            reason = "；".join(warnings)
        else:
            disposition = "valid"
            reason = None
        parsed_business_date = parsed_date.value if isinstance(parsed_date.value, date) else None
        row = NormalizedRow(
            row_id=f"{table.filename}:{table.sheet}:{offset}",
            side=table.role,
            business_date=parsed_business_date,
            month=parsed_business_date.strftime("%Y-%m") if parsed_business_date else None,
            floor=floor.value if isinstance(floor.value, str) else None,
            room=room.value if isinstance(room.value, str) else None,
            category=category.value if isinstance(category.value, str) else None,
            amount=amount.value if isinstance(amount.value, Decimal) else None,
            source_filename=table.filename,
            source_sheet=table.sheet,
            source_row_number=offset,
            raw_values={"date": raw_date, "floor": raw_floor, "room": _cell(source, table.columns, "room"), "category": raw_category, "amount": raw_amount},
            customer_name=str(_cell(source, table.columns, "customer") or "") or None,
            warnings=warnings,
            disposition=disposition,
            exclusion_reason=reason,
        )
        buckets[disposition].append(row)
    return NormalizationResult(buckets["valid"], buckets["excluded"], buckets["unparseable"])
