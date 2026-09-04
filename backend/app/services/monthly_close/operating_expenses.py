"""Deterministic, idempotent import of archived operating-expense workbooks."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from hashlib import sha256
import re
import unicodedata

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models.expense import (
    EXPENSE_CATEGORY_LABELS,
    Expense,
    ExpenseCategory,
    ExpensePayer,
)
from app.models.monthly_close import MonthlyCloseCycle, MonthlyCloseDocument
from app.models.room import Room
from app.services.audit import log_action_tx
from app.services.billing_recon.parser import BillParseError, load_workbook_rows
from app.services.monthly_close.documents import MonthlyCloseDocumentError


_TEMPLATE_MARKER = "__TEMPLATE_SAMPLE__"
_ALIASES = {
    "date": {"日期", "支出日期", "费用日期"},
    "category": {"类别", "费用类别", "支出类别", "项目"},
    "amount": {"金额", "支出金额", "费用", "合计金额"},
    "description": {"描述", "说明", "费用说明", "摘要"},
    "room": {"关联房号", "房号", "房间", "房间号"},
    "payer": {"支付方", "承担方", "付款方"},
    "notes": {"备注", "附注"},
}
_FUZZY_CATEGORIES = (
    ("厨房保洁", ExpenseCategory.kitchen_cleaning),
    ("保洁", ExpenseCategory.cleaning),
    ("维修", ExpenseCategory.maintenance),
    ("公摊", ExpenseCategory.public_utilities),
    ("新布草", ExpenseCategory.new_linen_prewash),
    ("水费", ExpenseCategory.water),
    ("水电", ExpenseCategory.utilities),
    ("电费", ExpenseCategory.electricity),
    ("燃气", ExpenseCategory.gas),
    ("宽带", ExpenseCategory.broadband),
    ("日耗", ExpenseCategory.daily_supplies),
    ("洗涤", ExpenseCategory.laundry),
    ("物业引导", ExpenseCategory.property_guidance_fee),
    ("物业", ExpenseCategory.property_fee),
    ("采购", ExpenseCategory.supplies),
    ("物品", ExpenseCategory.supplies),
    ("平台", ExpenseCategory.platform_fee),
    ("佣金", ExpenseCategory.platform_fee),
    ("税", ExpenseCategory.tax),
    ("其他", ExpenseCategory.other),
)


@dataclass(frozen=True)
class OperatingExpenseMapping:
    sheet: str
    header_row: int
    columns: dict[str, int]
    category_values: dict[str, str] = field(default_factory=dict)
    payer_values: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ParsedOperatingExpenseRow:
    row_number: int
    expense_date: date
    category: ExpenseCategory
    amount: Decimal
    description: str
    room_id: str | None
    payer: ExpensePayer
    notes: str | None
    business_key: str


@dataclass(frozen=True)
class ParsedOperatingExpenseWorkbook:
    mapping: OperatingExpenseMapping
    total_rows: int
    rows: list[ParsedOperatingExpenseRow]
    failed: list[dict]


def _text(value) -> str:
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value or "").strip()


def _normalized(value) -> str:
    return "".join(_text(value).lower().split())


def _detect_header(sheets: dict[str, list[list]]) -> tuple[str, int, dict[str, int]]:
    best = None
    for sheet_name, rows in sheets.items():
        for row_index, row in enumerate(rows[:30]):
            columns: dict[str, int] = {}
            values = [_normalized(value) for value in row]
            for field, aliases in _ALIASES.items():
                expected = {_normalized(alias) for alias in aliases}
                for column_index, value in enumerate(values):
                    if value in expected:
                        columns[field] = column_index
                        break
            if {"date", "category", "amount", "description"}.issubset(columns):
                candidate = (len(columns), -row_index, sheet_name, columns)
                if best is None or candidate[:2] > best[:2]:
                    best = candidate
    if best is None:
        raise MonthlyCloseDocumentError(
            "expense_layout_unknown",
            "无法可靠识别运营支出表头，请使用日期、类别、金额、描述列。",
            422,
        )
    _, negative_row, sheet_name, columns = best
    return sheet_name, -negative_row, columns


def _validate_mapping(
    mapping: OperatingExpenseMapping,
    sheets: dict[str, list[list]],
) -> OperatingExpenseMapping:
    if mapping.sheet not in sheets:
        raise MonthlyCloseDocumentError(
            "expense_mapping_invalid", "确认的工作表不存在。", 422
        )
    rows = sheets[mapping.sheet]
    if mapping.header_row < 0 or mapping.header_row >= len(rows):
        raise MonthlyCloseDocumentError(
            "expense_mapping_invalid", "确认的表头行超出范围。", 422
        )
    unknown = set(mapping.columns) - set(_ALIASES)
    required = {"date", "category", "amount", "description"}
    indexes = list(mapping.columns.values())
    if unknown or not required <= set(mapping.columns):
        raise MonthlyCloseDocumentError(
            "expense_mapping_invalid", "确认的字段映射不完整。", 422
        )
    if any(not isinstance(index, int) or index < 0 for index in indexes):
        raise MonthlyCloseDocumentError(
            "expense_mapping_invalid", "确认的列号无效。", 422
        )
    if len(indexes) != len(set(indexes)):
        raise MonthlyCloseDocumentError(
            "expense_mapping_invalid", "一个列不能同时映射多个字段。", 422
        )
    if indexes and max(indexes) >= len(rows[mapping.header_row]):
        raise MonthlyCloseDocumentError(
            "expense_mapping_invalid", "确认的列号超出表头范围。", 422
        )
    return mapping


def _cell(row: list, index: int | None):
    if index is None or index >= len(row) or row[index] is None:
        return ""
    return row[index]


def _parse_date(value, row_number: int, billing_month: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raw = _text(value)
    for date_format in (
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y年%m月%d日",
        "%m/%d/%Y",
    ):
        try:
            return datetime.strptime(raw, date_format).date()
        except ValueError:
            continue
    billing_year, billing_month_number = (
        int(part) for part in billing_month.split("-", 1)
    )
    short_date = re.fullmatch(r"(?P<month>\d{1,2})(?:[/.-]|月)(?P<day>\d{1,2})(?:日)?", raw)
    if short_date:
        parsed_month = int(short_date.group("month"))
        parsed_day = int(short_date.group("day"))
        if parsed_month == billing_month_number:
            try:
                return date(billing_year, parsed_month, parsed_day)
            except ValueError:
                pass
    raise ValueError(f"第{row_number}行日期格式错误")


def _parse_category(
    value,
    row_number: int,
    value_map: dict[str, str] | None = None,
) -> ExpenseCategory:
    raw = _text(value)
    normalized_map = {_normalized(key): mapped for key, mapped in (value_map or {}).items()}
    mapped = normalized_map.get(_normalized(raw))
    if mapped is not None:
        try:
            return ExpenseCategory(mapped)
        except ValueError:
            raise ValueError(f"第{row_number}行类别映射无效: {raw}") from None
    lowered = raw.lower()
    try:
        return ExpenseCategory(lowered)
    except ValueError:
        pass
    exact = {
        label.replace("（旧）", "").replace("(旧)", ""): category
        for category, label in EXPENSE_CATEGORY_LABELS.items()
    }
    if raw in exact:
        return exact[raw]
    for token, category in _FUZZY_CATEGORIES:
        if token in raw:
            return category
    raise ValueError(f"第{row_number}行未知类别: {raw}")


def _parse_amount(value, row_number: int) -> Decimal:
    raw = _text(value).replace(",", "").replace("¥", "").replace("￥", "")
    try:
        amount = Decimal(raw).quantize(Decimal("0.01"))
    except InvalidOperation as exc:
        raise ValueError(f"第{row_number}行金额格式错误") from exc
    if not amount.is_finite() or amount <= 0:
        raise ValueError(f"第{row_number}行金额必须大于0")
    return amount


def _parse_payer(
    value,
    row_number: int,
    value_map: dict[str, str] | None = None,
) -> ExpensePayer:
    raw = _text(value)
    if not raw:
        return ExpensePayer.company
    normalized = "".join(raw.lower().split())
    normalized_map = {
        "".join(str(key).lower().split()): mapped
        for key, mapped in (value_map or {}).items()
    }
    mapped = normalized_map.get(normalized)
    if mapped is not None:
        try:
            return ExpensePayer(mapped)
        except ValueError:
            raise ValueError(f"第{row_number}行支付方映射无效: {raw}") from None
    owner_aliases = {
        "owner",
        "业主",
        "业主承担",
        "业主支付",
        "房东",
        "房东承担",
        "房东支付",
        "房主",
        "房主承担",
        "房主支付",
    }
    company_aliases = {
        "company",
        "公司",
        "公司承担",
        "公司支付",
        "民宿",
        "民宿承担",
        "民宿支付",
    }
    if normalized in owner_aliases:
        return ExpensePayer.owner
    if normalized in company_aliases:
        return ExpensePayer.company
    raise ValueError(f"第{row_number}行无法判断支付方: {raw}")


def _room_key(value: object) -> str:
    normalized = unicodedata.normalize("NFKC", _text(value)).casefold()
    return "".join(normalized.split())


def build_room_aliases(room_rows: list[tuple[str, str]]) -> dict[str, str]:
    aliases: dict[str, str] = {}
    ambiguous: set[str] = set()
    for room_id, room_name in room_rows:
        room_id_key = _room_key(room_id)
        stripped = room_id_key
        for prefix in ("room", "房间"):
            if stripped.startswith(prefix):
                stripped = stripped.removeprefix(prefix)
                break
        if stripped.startswith("r") and stripped[1:].isdigit():
            stripped = stripped[1:]
        keys = {
            room_id_key,
            _room_key(room_name),
            stripped,
            f"r{stripped}" if stripped else "",
            f"room{stripped}" if stripped else "",
            f"房间{stripped}" if stripped else "",
            f"{stripped}号房" if stripped else "",
        } - {""}
        for key in keys:
            existing = aliases.get(key)
            if existing is not None and existing != room_id:
                ambiguous.add(key)
            else:
                aliases[key] = room_id
    for key in ambiguous:
        aliases.pop(key, None)
    return aliases


def _normalize_room(
    value,
    valid_ids: set[str],
    room_aliases: dict[str, str] | None = None,
) -> str | None:
    raw = _text(value).upper()
    if not raw:
        return None
    if raw in valid_ids:
        return raw
    canonical = (room_aliases or {}).get(_room_key(value))
    if canonical is not None:
        return canonical
    stripped = raw.removeprefix("ROOM").removeprefix("房间").removeprefix("R").strip()
    for candidate in (stripped, f"R{stripped}"):
        if candidate in valid_ids:
            return candidate
    raise ValueError(f"房号未找到: {raw}")


def _expense_id(document: MonthlyCloseDocument, sheet: str, row_number: int) -> str:
    digest = sha256(
        f"{document.sha256}\x1f{sheet}\x1f{row_number}".encode("utf-8")
    ).hexdigest()[:16].upper()
    return f"EXM-{digest}"


def parse_operating_expense_workbook(
    data: bytes,
    filename: str,
    billing_month: str,
    *,
    valid_room_ids: set[str],
    room_aliases: dict[str, str] | None = None,
    mapping: OperatingExpenseMapping | None = None,
) -> ParsedOperatingExpenseWorkbook:
    """Parse and validate an expense workbook without writing financial data."""
    try:
        sheets, _ = load_workbook_rows(data, filename)
    except BillParseError as exc:
        raise MonthlyCloseDocumentError(
            "expense_workbook_invalid", "无法读取运营支出表格。", 422
        ) from exc
    if mapping is None:
        sheet_name, header_row, columns = _detect_header(sheets)
        mapping = OperatingExpenseMapping(sheet_name, header_row, columns)
    else:
        mapping = _validate_mapping(mapping, sheets)

    parsed_rows: list[ParsedOperatingExpenseRow] = []
    failed: list[dict] = []
    total_rows = 0
    rows = sheets[mapping.sheet]
    for row_index, row in enumerate(
        rows[mapping.header_row + 1 :], start=mapping.header_row + 1
    ):
        row_number = row_index + 1
        if not any(_text(value) for value in row):
            continue
        if len(row) > 7 and _text(row[7]) == _TEMPLATE_MARKER:
            continue
        total_rows += 1
        try:
            expense_date = _parse_date(
                _cell(row, mapping.columns["date"]), row_number, billing_month
            )
            if expense_date.strftime("%Y-%m") != billing_month:
                raise ValueError(f"第{row_number}行不属于{billing_month}")
            category = _parse_category(
                _cell(row, mapping.columns["category"]),
                row_number,
                mapping.category_values,
            )
            amount = _parse_amount(
                _cell(row, mapping.columns["amount"]), row_number
            )
            description = _text(_cell(row, mapping.columns["description"]))
            if not description:
                raise ValueError(f"第{row_number}行描述不能为空")
            room_id = _normalize_room(
                _cell(row, mapping.columns.get("room")),
                valid_room_ids,
                room_aliases,
            )
            payer = _parse_payer(
                _cell(row, mapping.columns.get("payer")),
                row_number,
                mapping.payer_values,
            )
            notes = _text(_cell(row, mapping.columns.get("notes"))) or None
            business_key = "|".join(
                (
                    expense_date.isoformat(),
                    category.value,
                    str(amount),
                    room_id or "",
                    description,
                )
            )
            parsed_rows.append(
                ParsedOperatingExpenseRow(
                    row_number=row_number,
                    expense_date=expense_date,
                    category=category,
                    amount=amount,
                    description=description,
                    room_id=room_id,
                    payer=payer,
                    notes=notes,
                    business_key=business_key,
                )
            )
        except ValueError as exc:
            failed.append({"row": row_number, "errors": str(exc)})
    return ParsedOperatingExpenseWorkbook(
        mapping=mapping,
        total_rows=total_rows,
        rows=parsed_rows,
        failed=failed,
    )


async def _import_document(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    document: MonthlyCloseDocument,
    user_id: str,
    *,
    mapping: OperatingExpenseMapping | None = None,
) -> dict:
    if (
        document.engine_type == "expense_import"
        and document.engine_id
        and document.processing_status == "processed"
    ):
        return dict((document.metadata_ or {}).get("import_result") or {})
    if mapping is None:
        stored_mapping = (document.metadata_ or {}).get("mapping")
        if (
            isinstance(stored_mapping, dict)
            and isinstance(stored_mapping.get("sheet"), str)
            and isinstance(stored_mapping.get("header_row"), int)
            and isinstance(stored_mapping.get("columns"), dict)
        ):
            mapping = OperatingExpenseMapping(
                sheet=stored_mapping["sheet"],
                header_row=stored_mapping["header_row"],
                columns=stored_mapping["columns"],
                category_values=stored_mapping.get("category_values", {}),
                payer_values=stored_mapping.get("payer_values", {}),
            )
    room_rows = list(
        (await db.execute(select(Room.room_id, Room.room_name))).tuples()
    )
    valid_room_ids = {room_id for room_id, _ in room_rows}
    parsed = parse_operating_expense_workbook(
        document.content,
        document.filename,
        cycle.billing_month,
        valid_room_ids=valid_room_ids,
        room_aliases=build_room_aliases(room_rows),
        mapping=mapping,
    )
    other_documents = list(
        (
            await db.execute(
                select(MonthlyCloseDocument).where(
                    MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                    MonthlyCloseDocument.source_type == "operating_expenses",
                    MonthlyCloseDocument.is_active.is_(True),
                    MonthlyCloseDocument.document_id != document.document_id,
                )
            )
        ).scalars()
    )
    existing_business_keys = {
        key
        for other in other_documents
        for key in (other.metadata_ or {}).get("expense_business_keys", [])
    }
    seen_business_keys: set[str] = set()
    imported_ids: list[str] = []
    business_keys: list[str] = []
    failed: list[dict] = list(parsed.failed)
    for item in parsed.rows:
        try:
            if (
                item.business_key in seen_business_keys
                or item.business_key in existing_business_keys
            ):
                raise ValueError(f"第{item.row_number}行与其他有效文件重复")
            seen_business_keys.add(item.business_key)
            expense_id = _expense_id(
                document, parsed.mapping.sheet, item.row_number
            )
            existing = await db.get(Expense, expense_id)
            if existing is None:
                db.add(
                    Expense(
                        expense_id=expense_id,
                        category=item.category,
                        amount=item.amount,
                        description=item.description[:200],
                        expense_date=item.expense_date,
                        room_id=item.room_id,
                        payer=item.payer,
                        notes=item.notes[:200] if item.notes else None,
                        created_by=user_id,
                    )
                )
            imported_ids.append(expense_id)
            business_keys.append(item.business_key)
        except ValueError as exc:
            failed.append({"row": item.row_number, "errors": str(exc)})
    batch_id = "EXI-" + document.sha256[:12].upper()
    result = {
        "document_id": document.document_id,
        "batch_id": batch_id,
        "total_rows": parsed.total_rows,
        "imported_count": len(imported_ids),
        "expense_ids": imported_ids,
        "failed": failed,
    }
    document.engine_type = "expense_import"
    document.engine_id = batch_id
    document.processing_status = "processed" if not failed else "rejected"
    document.processing_error = None if not failed else f"{len(failed)}行未导入"
    document.metadata_ = {
        **(document.metadata_ or {}),
        "expense_business_keys": business_keys,
        "import_result": result,
        "mapping": {
            "sheet": parsed.mapping.sheet,
            "header_row": parsed.mapping.header_row,
            "columns": parsed.mapping.columns,
            "category_values": parsed.mapping.category_values,
            "payer_values": parsed.mapping.payer_values,
        },
    }
    if not failed:
        from app.services.monthly_close.layout_memory import remember_document_mapping

        remember_document_mapping(
            document,
            data=document.content,
            filename=document.filename,
            mapping=parsed.mapping,
        )
    await log_action_tx(
        db,
        user_id,
        "monthly_close.operating_expenses.import",
        "monthly_close_document",
        document.document_id,
        after_data={
            "billing_month": cycle.billing_month,
            "batch_id": batch_id,
            "imported_count": len(imported_ids),
            "failed_count": len(failed),
        },
    )
    return result


async def import_operating_expense_document(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    document: MonthlyCloseDocument,
    user_id: str,
    *,
    mapping: OperatingExpenseMapping,
) -> dict:
    await db.refresh(document, attribute_names=["is_active"])
    if not document.is_active:
        raise MonthlyCloseDocumentError(
            "document_not_active",
            "该运营支出原件已归档，不能继续导入。",
            409,
        )
    result = await _import_document(
        db,
        cycle,
        document,
        user_id,
        mapping=mapping,
    )
    await db.commit()
    return result


async def import_operating_expense_documents(
    db: AsyncSession, cycle: MonthlyCloseCycle, user_id: str
) -> dict:
    documents = list(
        (
            await db.execute(
                select(MonthlyCloseDocument)
                .options(undefer(MonthlyCloseDocument.content))
                .where(
                    MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                    MonthlyCloseDocument.source_type == "operating_expenses",
                    MonthlyCloseDocument.is_active.is_(True),
                )
                .order_by(MonthlyCloseDocument.uploaded_at)
                .with_for_update()
            )
        ).scalars()
    )
    if not documents:
        raise MonthlyCloseDocumentError(
            "operating_expense_document_missing", "请先上传运营支出明细。", 409
        )
    results = [
        await _import_document(db, cycle, document, user_id)
        for document in documents
    ]
    await db.commit()
    return {
        "document_count": len(results),
        "total_rows": sum(item.get("total_rows", 0) for item in results),
        "imported_count": sum(item.get("imported_count", 0) for item in results),
        "failed": [failure for item in results for failure in item.get("failed", [])],
        "results": results,
    }
