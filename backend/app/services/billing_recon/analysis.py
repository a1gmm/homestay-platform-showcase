"""平台无关的账单布局分析。

这里的函数只读取已加载的工作簿数据：不会访问数据库、调用 AI 或创建对账批次。
布局签名刻意只保留结构和安全标签，不能把订单号、客人或金额带出文件边界。
"""
from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field

from app.services.billing_recon.parser import (
    BillMapping,
    BillParseError,
    RowTypeName,
    _cell,
    _dec_checked,
    _order_no,
    _parse_date,
    extract_bill_rows,
    validate_bill,
)

_SAFE_LABELS = (
    "订单", "订单号", "客人", "客人姓名", "姓名", "入住", "入住日期", "离店", "离店日期",
    "结算", "结算价", "结算金额", "金额", "付款", "实际付款金额", "房型", "类型", "订单类型",
    "退款", "赔款", "罚款", "正常", "佣金", "服务费", "合计", "汇总",
)
_ORDER_ID_RE = re.compile(r"^\d{8,}$")
_DATE_RE = re.compile(r"^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}")
_NUMBER_RE = re.compile(r"^[+-]?(?:¥\s*)?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_QUALITY_MINIMUM = 0.95
_NON_DETAIL_ORDER_LABELS = {
    "订单", "订单号", "备注", "说明", "合计", "总计", "小计", "汇总",
    "note", "notes", "summary", "total", "subtotal",
}
_NON_DETAIL_ORDER_KEYWORDS = ("备注", "提示", "说明", "合计", "总计", "小计", "汇总")


class _SummaryCellValueError(BillParseError):
    """The summary coordinate exists, but its value is unsuitable as a total."""


class PlatformScope(str, Enum):
    ctrip_family = "ctrip_family"
    meituan = "meituan"
    fliggy = "fliggy"
    douyin = "douyin"
    tujia = "tujia"
    all_ota = "all_ota"


class CellRef(BaseModel):
    sheet: str
    row: int = Field(ge=0)
    col: int = Field(ge=0)


class MappingCoordinates(BaseModel):
    sheet: str
    header_row: int = Field(ge=0)
    col_order_no: int = Field(ge=0)
    col_guest: int = Field(ge=0)
    col_checkin: int = Field(ge=0)
    col_checkout: int = Field(ge=0)
    col_amount: int = Field(ge=0)
    col_row_type: int | None = Field(default=None, ge=0)
    row_type_map: dict[str, RowTypeName] = Field(default_factory=dict)
    summary_cell: CellRef | None = None


class MappingQuality(BaseModel):
    parsed_row_count: int = Field(ge=0)
    order_no_parse_ratio: float = Field(ge=0, le=1)
    amount_parse_ratio: float = Field(ge=0, le=1)
    checkout_parse_ratio: float = Field(ge=0, le=1)
    computed_total: float
    independent_total_verified: bool


class MappingIssue(BaseModel):
    code: Literal[
        "MAPPING_INCOMPLETE", "MAPPING_OUT_OF_RANGE", "PARSE_QUALITY_LOW", "TOTAL_MISMATCH",
    ]
    message: str
    field: str | None = None


class MappingAnalysis(BaseModel):
    coordinates: MappingCoordinates
    quality: MappingQuality
    warnings: list[str] = Field(default_factory=list)
    errors: list[MappingIssue] = Field(default_factory=list)
    preview: list[dict[str, str | float | None]] = Field(default_factory=list)


def _cell_shape(value: object) -> str:
    """Return a structural, non-sensitive cell token for the layout signature."""
    if value is None or str(value).strip() == "":
        return "EMPTY"
    if isinstance(value, (datetime, date)):
        return "DATE"
    if isinstance(value, (int, float, Decimal)):
        return "NUMBER"
    text = str(value).strip()
    if _ORDER_ID_RE.fullmatch(text):
        return "ORDER_ID"
    if _DATE_RE.match(text):
        return "DATE"
    if _NUMBER_RE.fullmatch(text):
        return "NUMBER"
    if text in _SAFE_LABELS:
        return text
    return "TEXT"


def layout_signature_payload(sheets: dict[str, list[list]]) -> str:
    """Serialize stable layout data, excluding every instance/detail value.

    Templates need to survive a different month's row count and values.  Safe
    label positions preserve headers (including mixed label/constant rows), while
    order-detail rows contribute only normalized type patterns.  Exact detail
    values and the number/order of repeated rows never enter the payload.
    """
    structural_sheets = []
    for rows in sheets.values():
        labels = []
        data_patterns: set[tuple[str, ...]] = set()
        for row_index, row in enumerate(rows):
            shapes = [_cell_shape(cell) for cell in row]
            safe_cells = [
                [col_index, shape]
                for col_index, shape in enumerate(shapes)
                if shape in _SAFE_LABELS
            ]
            has_order_id = "ORDER_ID" in shapes
            has_date = "DATE" in shapes
            has_number = "NUMBER" in shapes
            is_header = bool(safe_cells) and (
                len(safe_cells) >= 2 or not (has_order_id or has_date or has_number)
            )
            if is_header:
                labels.append([row_index, safe_cells])
            elif has_order_id:
                data_patterns.add(tuple(
                    "TEXT" if shape in _SAFE_LABELS else shape
                    for shape in shapes
                ))
        structural_sheets.append({
            "columns": max((len(row) for row in rows), default=0),
            "labels": labels,
            "data_patterns": [list(pattern) for pattern in sorted(data_patterns)],
        })
    payload = {
        "sheets": structural_sheets,
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def build_layout_signature(sheets: dict[str, list[list]]) -> str:
    return hashlib.sha256(layout_signature_payload(sheets).encode("utf-8")).hexdigest()


def _summary_total(sheets: dict[str, list[list]], coordinates: MappingCoordinates) -> float | None:
    ref = coordinates.summary_cell
    if ref is None:
        return None
    rows = sheets.get(ref.sheet)
    if rows is None or ref.row >= len(rows) or ref.col >= len(rows[ref.row]):
        raise BillParseError("独立汇总金额单元格越界")
    if ref.sheet == coordinates.sheet and _order_no(_cell(rows[ref.row], coordinates.col_order_no)) is not None:
        return None
    amount, unparsed = _dec_checked(rows[ref.row][ref.col])
    if unparsed:
        raise _SummaryCellValueError("独立汇总金额不是合法数字")
    return float(amount)


def materialize_mapping(sheets: dict[str, list[list]], coordinates: MappingCoordinates) -> BillMapping:
    """Attach this workbook's optional independent total to reusable coordinates."""
    return BillMapping(
        sheet=coordinates.sheet,
        header_row=coordinates.header_row,
        col_order_no=coordinates.col_order_no,
        col_guest=coordinates.col_guest,
        col_checkin=coordinates.col_checkin,
        col_checkout=coordinates.col_checkout,
        col_amount=coordinates.col_amount,
        col_row_type=coordinates.col_row_type,
        row_type_map=coordinates.row_type_map,
        summary_total=_summary_total(sheets, coordinates),
    )


def _summary_cell_is_detail(sheets: dict[str, list[list]], coordinates: MappingCoordinates) -> bool:
    ref = coordinates.summary_cell
    if ref is None or ref.sheet != coordinates.sheet:
        return False
    rows = sheets.get(ref.sheet)
    return bool(
        rows
        and ref.row < len(rows)
        and _order_no(_cell(rows[ref.row], coordinates.col_order_no)) is not None
    )


def _mapping_error(coordinates: MappingCoordinates, sheets: dict[str, list[list]]) -> MappingIssue | None:
    rows = sheets.get(coordinates.sheet)
    if rows is None:
        return MappingIssue(code="MAPPING_OUT_OF_RANGE", field="sheet", message="选择的工作表不存在")
    if coordinates.header_row >= len(rows):
        return MappingIssue(code="MAPPING_OUT_OF_RANGE", field="header_row", message="表头行号越界")
    header = rows[coordinates.header_row]
    fields = {
        "col_order_no": coordinates.col_order_no,
        "col_guest": coordinates.col_guest,
        "col_checkin": coordinates.col_checkin,
        "col_checkout": coordinates.col_checkout,
        "col_amount": coordinates.col_amount,
    }
    if coordinates.col_row_type is not None:
        fields["col_row_type"] = coordinates.col_row_type
    for field, col in fields.items():
        if col >= len(header):
            return MappingIssue(code="MAPPING_OUT_OF_RANGE", field=field, message="字段列号越界")
    return None


def _mask_order_no(order_no: str) -> str:
    return f"{order_no[:2]}****{order_no[-2:]}" if len(order_no) > 4 else "****"


def _mask_guest(guest: str) -> str:
    return f"{guest[:1]}*" if guest else ""


def _ratio(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _normalized_label(value: object) -> str:
    """Normalize structural labels without retaining them outside this call."""
    text = unicodedata.normalize("NFKC", str(value)).strip().casefold()
    start = 0
    end = len(text)
    while start < end and (text[start].isspace() or unicodedata.category(text[start]).startswith("P")):
        start += 1
    while end > start and (text[end - 1].isspace() or unicodedata.category(text[end - 1]).startswith("P")):
        end -= 1
    return text[start:end]


def _mapped_business_labels(row: list, coordinates: MappingCoordinates) -> tuple[str, ...]:
    return tuple(_normalized_label(_cell(row, col)) for col in (
        coordinates.col_order_no,
        coordinates.col_guest,
        coordinates.col_checkin,
        coordinates.col_checkout,
        coordinates.col_amount,
    ))


def _is_non_detail_order_label(raw_order: object) -> bool:
    label = _normalized_label(raw_order)
    if label in _NON_DETAIL_ORDER_LABELS:
        return True
    if any(keyword in label for keyword in _NON_DETAIL_ORDER_KEYWORDS):
        return True
    english_words = set(re.findall(r"[a-z]+", label))
    return bool(english_words & {"note", "notes", "summary", "total", "subtotal"})


def _is_candidate_detail_row(
    row: list, coordinates: MappingCoordinates, datemode: int, header: list,
) -> bool:
    """Recognize mapped business rows without requiring an order identifier.

    Repeated mapped headers and normalized note/total labels are rejected before
    density checks.  A valid order number is sufficient.  Missing or malformed
    identifiers are retained when either a real stay date has a companion value
    or the mapped business columns are densely populated.  Known structural and
    sparse footer rows remain excluded.
    """
    mapped_labels = _mapped_business_labels(row, coordinates)
    header_labels = _mapped_business_labels(header, coordinates)
    if any(mapped_labels) and mapped_labels == header_labels:
        return False
    raw_order = str(_cell(row, coordinates.col_order_no)).strip()
    if _is_non_detail_order_label(raw_order):
        return False
    if _order_no(raw_order) is not None:
        return True
    companion_values = (
        _cell(row, coordinates.col_guest),
        _cell(row, coordinates.col_checkin),
        _cell(row, coordinates.col_checkout),
        _cell(row, coordinates.col_amount),
    )
    nonempty_companions = sum(bool(str(value).strip()) for value in companion_values)
    checkin = _parse_date(_cell(row, coordinates.col_checkin), datemode)
    checkout = _parse_date(_cell(row, coordinates.col_checkout), datemode)
    if (checkin is not None or checkout is not None) and nonempty_companions >= 2:
        return True
    if raw_order:
        return nonempty_companions >= 2
    return nonempty_companions >= 3


def _empty_analysis(coordinates: MappingCoordinates, errors: list[MappingIssue]) -> MappingAnalysis:
    return MappingAnalysis(
        coordinates=coordinates,
        quality=MappingQuality(
            parsed_row_count=0, order_no_parse_ratio=0.0, amount_parse_ratio=0.0,
            checkout_parse_ratio=0.0, computed_total=0.0, independent_total_verified=False,
        ),
        errors=errors,
    )


def analyze_mapping(
    sheets: dict[str, list[list]], datemode: int, coordinates: MappingCoordinates,
) -> MappingAnalysis:
    """Run local parsing/quality checks and produce a masked preview for review."""
    coordinate_error = _mapping_error(coordinates, sheets)
    if coordinate_error:
        return _empty_analysis(coordinates, [coordinate_error])

    try:
        mapping = materialize_mapping(sheets, coordinates)
    except _SummaryCellValueError:
        return _empty_analysis(coordinates, [
            MappingIssue(
                code="PARSE_QUALITY_LOW", field="summary_cell", message="独立汇总金额无法解析",
            ),
        ])
    except BillParseError as exc:
        return _empty_analysis(coordinates, [
            MappingIssue(code="MAPPING_OUT_OF_RANGE", field="summary_cell", message=str(exc)),
        ])

    rows = sheets[coordinates.sheet]
    source_rows = rows[coordinates.header_row + 1:]
    if coordinates.summary_cell and coordinates.summary_cell.sheet == coordinates.sheet:
        summary_row = coordinates.summary_cell.row - coordinates.header_row - 1
        source_rows = [row for index, row in enumerate(source_rows) if index != summary_row]
    candidate_rows = [
        row for row in source_rows
        if _is_candidate_detail_row(
            row, coordinates, datemode, rows[coordinates.header_row],
        )
    ]
    parsed_rows = extract_bill_rows(rows, mapping, datemode)
    valid_order_count = sum(_order_no(_cell(row, coordinates.col_order_no)) is not None for row in candidate_rows)
    amount_ok = sum(
        not _dec_checked(_cell(row, coordinates.col_amount))[1]
        for row in candidate_rows
    )
    checkout_ok = sum(
        _parse_date(_cell(row, coordinates.col_checkout), datemode) is not None
        for row in candidate_rows
    )
    errors, stats = validate_bill(parsed_rows, mapping)

    quality = MappingQuality(
        parsed_row_count=len(parsed_rows),
        order_no_parse_ratio=_ratio(valid_order_count, len(candidate_rows)),
        amount_parse_ratio=_ratio(amount_ok, len(candidate_rows)),
        checkout_parse_ratio=_ratio(checkout_ok, len(candidate_rows)),
        computed_total=float(stats["computed_total"]),
        independent_total_verified=bool(stats["independent_total_verified"]),
    )
    analysis_errors: list[MappingIssue] = []
    if not parsed_rows:
        analysis_errors.append(MappingIssue(
            code="MAPPING_INCOMPLETE", field="col_order_no", message="没有解析出任何明细行",
        ))
    elif (
        quality.amount_parse_ratio < 1.0
        or quality.order_no_parse_ratio < _QUALITY_MINIMUM
        or quality.checkout_parse_ratio < _QUALITY_MINIMUM
    ):
        analysis_errors.append(MappingIssue(
            code="PARSE_QUALITY_LOW", message="金额必须全部可解析，订单号和离店日期解析率不得低于 95%",
        ))
    if any("逐行加总" in error for error in errors):
        analysis_errors.append(MappingIssue(
            code="TOTAL_MISMATCH",
            # validate_bill() retains exact amounts for the confirmation/engine path,
            # but analysis responses must never echo workbook values.
            message="独立汇总与明细合计不一致",
        ))
    elif errors:
        # Keep deterministic validation internally, while exposing a stable
        # content-free review issue to the browser.
        analysis_errors.append(MappingIssue(
            code="PARSE_QUALITY_LOW", message="账单数据未通过本地校验",
        ))

    preview = [
        {
            "order_no": _mask_order_no(row.order_no),
            "guest": _mask_guest(row.guest),
            "checkin": row.checkin.isoformat() if row.checkin else None,
            "checkout": row.checkout.isoformat() if row.checkout else None,
            "amount": float(row.amount),
            "row_type": row.row_type,
        }
        for row in parsed_rows[:3]
    ]
    warnings = [] if mapping.summary_total is not None else ["NO_INDEPENDENT_TOTAL"]
    if _summary_cell_is_detail(sheets, coordinates):
        warnings.append("SUMMARY_CELL_IS_DETAIL")
    return MappingAnalysis(
        coordinates=coordinates, quality=quality, warnings=warnings,
        errors=analysis_errors, preview=preview,
    )
