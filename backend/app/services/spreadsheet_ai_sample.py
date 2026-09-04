"""Privacy-preserving workbook samples for AI column mapping.

The model needs natural header wording to understand unfamiliar spreadsheets,
but never needs detail-row values. This module exposes exact short labels only
on likely header rows (or from an explicit safe-label allowlist) and replaces
all other cells with structural types.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
import re


_DATE_RE = re.compile(r"^\d{4}[-/.年]\d{1,2}[-/.月]\d{1,2}(?:日)?")
_SHORT_DATE_RE = re.compile(r"^\d{1,2}(?:[-/.]\d{1,2}|月\d{1,2}日)$")
_NUMBER_RE = re.compile(r"^[+-]?(?:[¥￥]\s*)?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?$")
_ORDER_ID_RE = re.compile(r"^\d{8,}$")
_SENSITIVE_RE = re.compile(r"(?:@|\b1\d{10}\b|\d{6,})")
_INSTRUCTION_RE = re.compile(
    r"(?:ignore|instruction|prompt|system|assistant|忽略|指令|提示词|系统消息)",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class SafeWorkbookStructure:
    rows_by_token: dict[str, list[list[str]]]
    sheet_tokens: dict[str, str]


def structural_cell_type(value: object) -> str:
    if value is None or str(value).strip() == "":
        return "EMPTY"
    if isinstance(value, (date, datetime)):
        return "DATE"
    if isinstance(value, (int, float, Decimal)) and not isinstance(value, bool):
        return "NUMBER"
    text = str(value).strip()
    if _ORDER_ID_RE.fullmatch(text):
        return "ORDER_ID"
    if _DATE_RE.match(text) or _SHORT_DATE_RE.fullmatch(text):
        return "DATE"
    if _NUMBER_RE.fullmatch(text):
        return "NUMBER"
    return "TEXT"


def _likely_header_rows(rows: list[list] | list[tuple], max_rows: int) -> set[int]:
    candidates: set[int] = set()
    for row_index, row in enumerate(rows[:max_rows]):
        nonempty = [value for value in row if str(value or "").strip()]
        if len(nonempty) < 2:
            continue
        current_types = {structural_cell_type(value) for value in nonempty}
        if current_types.intersection({"DATE", "NUMBER", "ORDER_ID"}):
            continue
        following = rows[row_index + 1 : row_index + 4]
        if any(
            "DATE" in {structural_cell_type(value) for value in next_row}
            and bool(
                {"NUMBER", "ORDER_ID"}
                & {structural_cell_type(value) for value in next_row}
            )
            for next_row in following
        ):
            candidates.add(row_index)
    return candidates


def _safe_label(value: object) -> str:
    text = " ".join(str(value or "").strip().split())
    if (
        not text
        or len(text) > 30
        or _SENSITIVE_RE.search(text)
        or _INSTRUCTION_RE.search(text)
    ):
        return "TEXT"
    return text


def build_safe_workbook_structure(
    sheets: dict[str, list[list] | list[tuple]],
    *,
    safe_labels: set[str] | None = None,
    max_rows: int = 30,
    max_columns: int = 100,
) -> SafeWorkbookStructure:
    allowed = {" ".join(label.strip().split()) for label in (safe_labels or set())}
    rows_by_token: dict[str, list[list[str]]] = {}
    sheet_tokens: dict[str, str] = {}
    for sheet_index, (sheet_name, rows) in enumerate(sheets.items(), 1):
        token = f"S{sheet_index}"
        sheet_tokens[token] = sheet_name
        header_rows = _likely_header_rows(rows, max_rows)
        rows_by_token[token] = [
            [
                _safe_label(value)
                if row_index in header_rows or str(value or "").strip() in allowed
                else structural_cell_type(value)
                for value in row[:max_columns]
            ]
            for row_index, row in enumerate(rows[:max_rows])
        ]
    return SafeWorkbookStructure(
        rows_by_token=rows_by_token,
        sheet_tokens=sheet_tokens,
    )
