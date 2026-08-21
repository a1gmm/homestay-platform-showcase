from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Any, Literal


WorkbookRole = Literal["receipt", "expense"]


@dataclass(frozen=True)
class WorkbookInput:
    filename: str
    content: bytes


@dataclass(frozen=True)
class DetectedTable:
    role: WorkbookRole
    filename: str
    sheet: str
    header_row: int
    columns: dict[str, int]
    rows: list[tuple]
    months: list[str]


@dataclass(frozen=True)
class InspectedFile:
    filename: str
    role: WorkbookRole
    sheets: list[DetectedTable]
    months: list[str]
    mapping_status: str = "mapped"


@dataclass(frozen=True)
class PreflightResult:
    files: list[InspectedFile]
    common_months: list[str]
    receipt_only_months: list[str] = field(default_factory=list)
    expense_only_months: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class NormalizedRow:
    row_id: str
    side: WorkbookRole
    business_date: date | None
    month: str | None
    floor: str | None
    room: str | None
    category: str | None
    amount: Decimal | None
    source_filename: str
    source_sheet: str
    source_row_number: int
    raw_values: dict[str, Any]
    customer_name: str | None = None
    warnings: tuple[str, ...] = ()
    disposition: Literal["valid", "excluded", "unparseable"] = "valid"
    exclusion_reason: str | None = None


@dataclass(frozen=True)
class NormalizationResult:
    valid: list[NormalizedRow]
    excluded: list[NormalizedRow]
    unparseable: list[NormalizedRow]
