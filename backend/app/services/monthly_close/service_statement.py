"""Deterministic parsing for cleaning and linen vendor statements.

The parser intentionally does not guess unknown columns.  AI may propose a
mapping in a later UI flow, but persisted service lines are always produced by
this deterministic boundary.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import re
from typing import Any

from app.services.billing_recon.parser import BillParseError, load_workbook_rows
from app.services.spreadsheet_ai_sample import build_safe_workbook_structure


_MONEY = Decimal("0.01")
_QUANTITY = Decimal("0.001")
_DATE_FORMATS = (
    "%Y-%m-%d",
    "%Y/%m/%d",
    "%Y.%m.%d",
    "%Y-%m-%d %H:%M:%S",
    "%Y/%m/%d %H:%M:%S",
    "%Y年%m月%d日",
)

_HEADER_ALIASES = {
    "date": {"服务日期", "日期", "打扫日期", "洗涤日期", "service date", "date"},
    "room": {"房号", "房间", "房间号", "房源", "room", "room no", "unit"},
    "order": {"订单号", "订单编号", "平台订单号", "order", "order id", "booking id"},
    "service": {"服务项目", "服务类型", "项目", "类别", "service", "service type", "item"},
    "quantity": {"数量", "次数", "套数", "quantity", "qty"},
    "unit_price": {"单价", "服务单价", "unit price", "price"},
    "amount": {"合计金额", "金额", "费用", "应付金额", "amount", "total", "total amount"},
}

_SERVICE_TYPES = {
    "退房保洁": "cleaning",
    "保洁": "cleaning",
    "退房清洁": "cleaning",
    "续住打扫": "instay_cleaning",
    "续住保洁": "instay_cleaning",
    "布草洗涤": "laundry",
    "布草": "laundry",
    "洗涤": "laundry",
}


class ServiceStatementError(ValueError):
    """The vendor workbook cannot be converted without guessing."""


@dataclass(frozen=True)
class ServiceStatementMapping:
    sheet: str
    header_row: int
    columns: dict[str, int]


@dataclass(frozen=True)
class ServiceStatementLine:
    source_sheet: str
    source_row_number: int
    service_date: date
    room_ref: str
    order_ref: str | None
    service_type: str
    quantity: Decimal | None
    unit_price: Decimal | None
    amount: Decimal
    business_key: str
    raw_values: dict[str, Any]


@dataclass(frozen=True)
class ParsedServiceStatement:
    mapping: ServiceStatementMapping
    lines: list[ServiceStatementLine]


@dataclass(frozen=True)
class AnonymousServiceSample:
    payload: str
    sheet_tokens: dict[str, str]


def _normalized_header(value: object) -> str:
    return "".join(str(value or "").strip().lower().split())


def _mapping_for_row(row: list[object]) -> dict[str, int]:
    normalized = [_normalized_header(value) for value in row]
    result: dict[str, int] = {}
    for field, aliases in _HEADER_ALIASES.items():
        expected = {_normalized_header(alias) for alias in aliases}
        for index, value in enumerate(normalized):
            if value in expected:
                result[field] = index
                break
    return result


def _mapping_is_usable(columns: dict[str, int]) -> bool:
    has_amount = "amount" in columns
    can_compute_amount = "quantity" in columns and "unit_price" in columns
    return "date" in columns and "room" in columns and (
        has_amount or can_compute_amount
    )


def _detect_mapping(sheets: dict[str, list[list]]) -> ServiceStatementMapping:
    candidates: list[tuple[int, int, str, dict[str, int]]] = []
    for sheet, rows in sheets.items():
        for row_index, row in enumerate(rows[:30]):
            columns = _mapping_for_row(row)
            if _mapping_is_usable(columns):
                candidates.append((len(columns), -row_index, sheet, columns))
    if not candidates:
        raise ServiceStatementError("无法可靠识别表头")
    _, negative_row, sheet, columns = max(candidates, key=lambda item: (item[0], item[1]))
    return ServiceStatementMapping(
        sheet=sheet,
        header_row=-negative_row,
        columns=columns,
    )


def _cell(row: list, index: int | None) -> object:
    if index is None or index < 0 or index >= len(row) or row[index] is None:
        return ""
    return row[index]


def _text(value: object) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value or "").strip()


def _decimal(value: object, row_number: int, label: str, quantum: Decimal) -> Decimal:
    raw = _text(value).replace(",", "").replace("¥", "").replace("￥", "")
    if not raw:
        raise ServiceStatementError(f"第{row_number}行{label}为空")
    try:
        parsed = Decimal(raw)
    except InvalidOperation as exc:
        raise ServiceStatementError(f"第{row_number}行{label}无法解析") from exc
    if not parsed.is_finite():
        raise ServiceStatementError(f"第{row_number}行{label}无法解析")
    return parsed.quantize(quantum, rounding=ROUND_HALF_UP)


def _date(
    value: object,
    row_number: int,
    datemode: int,
    billing_month: str | None,
) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and value > 0:
        import xlrd

        try:
            return xlrd.xldate_as_datetime(value, datemode).date()
        except Exception as exc:  # noqa: BLE001 - xlrd errors vary by workbook
            raise ServiceStatementError(f"第{row_number}行日期无法解析") from exc
    raw = _text(value)
    for date_format in _DATE_FORMATS:
        try:
            return datetime.strptime(raw, date_format).date()
        except ValueError:
            continue
    short_date = re.fullmatch(
        r"(?P<month>\d{1,2})(?:[/.-]|月)(?P<day>\d{1,2})(?:日)?",
        raw,
    )
    if short_date and billing_month:
        billing_year, billing_month_number = (
            int(part) for part in billing_month.split("-", 1)
        )
        parsed_month = int(short_date.group("month"))
        parsed_day = int(short_date.group("day"))
        if parsed_month == billing_month_number:
            try:
                return date(billing_year, parsed_month, parsed_day)
            except ValueError:
                pass
    raise ServiceStatementError(f"第{row_number}行日期无法解析")


def _service_type(value: object, source_type: str) -> str:
    raw = _text(value)
    if raw in _SERVICE_TYPES:
        return _SERVICE_TYPES[raw]
    if not raw:
        return "laundry" if source_type == "linen_statement" else "cleaning"
    if "续住" in raw:
        return "instay_cleaning"
    if "布草" in raw or "洗涤" in raw:
        return "laundry"
    if "保洁" in raw or "清洁" in raw or "打扫" in raw:
        return "cleaning"
    return "other_service"


def _validate_confirmed_mapping(
    mapping: ServiceStatementMapping, sheets: dict[str, list[list]]
) -> ServiceStatementMapping:
    if mapping.sheet not in sheets:
        raise ServiceStatementError("确认的工作表不存在")
    rows = sheets[mapping.sheet]
    if mapping.header_row < 0 or mapping.header_row >= len(rows):
        raise ServiceStatementError("确认的表头行超出范围")
    unknown = set(mapping.columns) - set(_HEADER_ALIASES)
    if unknown or not _mapping_is_usable(mapping.columns):
        raise ServiceStatementError("确认的字段映射不完整")
    indexes = list(mapping.columns.values())
    if any(not isinstance(index, int) or index < 0 for index in indexes):
        raise ServiceStatementError("确认的列号无效")
    if len(indexes) != len(set(indexes)):
        raise ServiceStatementError("一个列不能同时映射多个字段")
    if indexes and max(indexes) >= len(rows[mapping.header_row]):
        raise ServiceStatementError("确认的列号超出表头范围")
    return mapping


def parse_service_statement(
    data: bytes,
    filename: str,
    source_type: str,
    *,
    mapping: ServiceStatementMapping | None = None,
    billing_month: str | None = None,
) -> ParsedServiceStatement:
    if source_type not in {"cleaning_statement", "linen_statement"}:
        raise ServiceStatementError("文件类型不是保洁或布草对账单")
    try:
        sheets, datemode = load_workbook_rows(data, filename)
    except BillParseError as exc:
        raise ServiceStatementError("无法解析表格文件") from exc
    mapping = (
        _validate_confirmed_mapping(mapping, sheets)
        if mapping is not None
        else _detect_mapping(sheets)
    )
    rows = sheets[mapping.sheet]
    lines: list[ServiceStatementLine] = []
    for row_index, row in enumerate(
        rows[mapping.header_row + 1 :], start=mapping.header_row + 1
    ):
        row_number = row_index + 1
        if not any(_text(value) for value in row):
            continue
        room_ref = _text(_cell(row, mapping.columns.get("room")))
        raw_date = _cell(row, mapping.columns.get("date"))
        if not room_ref and not _text(raw_date):
            continue
        if not room_ref:
            raise ServiceStatementError(f"第{row_number}行房间为空")
        service_date = _date(raw_date, row_number, datemode, billing_month)
        if billing_month and service_date.strftime("%Y-%m") != billing_month:
            raise ServiceStatementError(
                f"第{row_number}行日期不属于{billing_month}"
            )
        quantity = None
        unit_price = None
        if "quantity" in mapping.columns:
            raw_quantity = _cell(row, mapping.columns["quantity"])
            if _text(raw_quantity):
                quantity = _decimal(raw_quantity, row_number, "数量", _QUANTITY)
        if "unit_price" in mapping.columns:
            raw_unit_price = _cell(row, mapping.columns["unit_price"])
            if _text(raw_unit_price):
                unit_price = _decimal(raw_unit_price, row_number, "单价", _MONEY)
        if "amount" in mapping.columns:
            amount = _decimal(
                _cell(row, mapping.columns["amount"]), row_number, "金额", _MONEY
            )
        elif quantity is not None and unit_price is not None:
            amount = (quantity * unit_price).quantize(_MONEY, rounding=ROUND_HALF_UP)
        else:
            raise ServiceStatementError(f"第{row_number}行金额无法计算")
        order_ref = _text(_cell(row, mapping.columns.get("order"))) or None
        service_type = _service_type(
            _cell(row, mapping.columns.get("service")), source_type
        )
        business_key = "|".join(
            (
                service_type,
                order_ref or "",
                room_ref,
                service_date.isoformat(),
            )
        )
        lines.append(
            ServiceStatementLine(
                source_sheet=mapping.sheet,
                source_row_number=row_number,
                service_date=service_date,
                room_ref=room_ref,
                order_ref=order_ref,
                service_type=service_type,
                quantity=quantity,
                unit_price=unit_price,
                amount=amount,
                business_key=business_key,
                raw_values={
                    "date": service_date.isoformat(),
                    "room": room_ref,
                    "order": order_ref,
                    "service_type": service_type,
                    "quantity": str(quantity) if quantity is not None else None,
                    "unit_price": str(unit_price) if unit_price is not None else None,
                    "amount": str(amount),
                },
            )
        )
    if not lines:
        raise ServiceStatementError("表格中没有可对账的服务明细")
    return ParsedServiceStatement(mapping=mapping, lines=lines)


def build_service_anonymous_sample(data: bytes, filename: str) -> AnonymousServiceSample:
    """Describe workbook shape without including filenames, sheet names, or cell values."""
    try:
        sheets, _ = load_workbook_rows(data, filename)
    except BillParseError as exc:
        raise ServiceStatementError("无法解析表格文件") from exc
    safe_labels = {alias for aliases in _HEADER_ALIASES.values() for alias in aliases}
    structure = build_safe_workbook_structure(sheets, safe_labels=safe_labels)
    payload = {
        "sheets": [
            {
                "sheet_token": token,
                "rows": rows,
            }
            for token, rows in structure.rows_by_token.items()
        ]
    }
    return AnonymousServiceSample(
        payload=json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
        sheet_tokens=structure.sheet_tokens,
    )
