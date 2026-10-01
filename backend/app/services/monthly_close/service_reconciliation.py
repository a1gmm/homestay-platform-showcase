"""Materialize and deterministically match cleaning/linen vendor lines."""

from __future__ import annotations

import unicodedata
from collections import defaultdict
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expense import Expense, ExpenseCategory
from app.models.monthly_close import MonthlyCloseDocument, MonthlyCloseServiceLine
from app.models.order import Order
from app.models.room import Room
from app.services.billing_recon.parser import BillParseError
from app.services.monthly_close.service_statement import (
    ServiceStatementMapping,
    ServiceStatementError,
    ServiceStatementLine,
    parse_service_statement,
)


_CATEGORY_BY_SERVICE_TYPE = {
    "cleaning": ExpenseCategory.cleaning,
    "instay_cleaning": ExpenseCategory.cleaning,
    "laundry": ExpenseCategory.laundry,
}


def _reference_key(value: str | None) -> str:
    """Normalize an external identifier without discarding punctuation."""
    normalized = unicodedata.normalize("NFKC", value or "").casefold()
    return "".join(normalized.split())


def _room_reference_keys(value: str) -> set[str]:
    """Return conservative room aliases while preserving ambiguity."""
    key = _reference_key(value)
    if not key:
        return set()
    keys = {key}
    stripped = key
    for prefix in ("room", "房间"):
        if stripped.startswith(prefix):
            stripped = stripped.removeprefix(prefix)
            break
    if stripped.startswith("r") and stripped[1:].isdigit():
        stripped = stripped[1:]
    for suffix in ("号房", "房", "号"):
        if stripped.endswith(suffix):
            stripped = stripped.removesuffix(suffix)
            break
    if stripped and stripped.isdigit():
        keys.update(
            {
                stripped,
                f"r{stripped}",
                f"room{stripped}",
                f"房间{stripped}",
                f"{stripped}号房",
            }
        )
    return keys


def _room_alias_index(room_rows: list[tuple[str, str]]) -> dict[str, set[str]]:
    aliases: dict[str, set[str]] = defaultdict(set)
    for room_id, room_name in room_rows:
        for alias in _room_reference_keys(room_id) | {_reference_key(room_name)}:
            if alias:
                aliases[alias].add(room_id)
    return aliases


def _resolve_room_reference(
    room_ref: str, aliases: dict[str, set[str]]
) -> tuple[str | None, tuple[str, ...], str]:
    candidates = sorted(
        {
            room_id
            for alias in _room_reference_keys(room_ref)
            for room_id in aliases.get(alias, set())
        }
    )
    if len(candidates) == 1:
        return candidates[0], tuple(candidates), "room_alias"
    if len(candidates) > 1:
        return None, tuple(candidates), "ambiguous_room_alias"
    return room_ref.strip(), (), "raw_room_reference"


def _match_expense(
    *,
    service_type: str,
    order_ref: str | None,
    room_ref: str,
    service_date,
    amount: Decimal,
    by_order: dict[tuple[ExpenseCategory, str, str], list[Expense]],
    by_date: dict[tuple[ExpenseCategory, str, object], list[Expense]],
) -> tuple[str, str | None, str | None, dict]:
    category = _CATEGORY_BY_SERVICE_TYPE.get(service_type)
    if category is None:
        return "vendor_only", None, "unknown_service_type", {}
    if order_ref:
        candidates = by_order.get((category, room_ref, _reference_key(order_ref)), [])
        match_method = "order_room_category"
    else:
        candidates = by_date.get((category, room_ref, service_date), [])
        match_method = "date_room_category"
    if not candidates:
        return "vendor_only", None, "system_expense_missing", {"match_method": match_method}
    if len(candidates) > 1:
        return (
            "ambiguous",
            None,
            "multiple_system_expenses",
            {
                "match_method": match_method,
                "candidate_expense_ids": sorted(item.expense_id for item in candidates),
            },
        )
    expense = candidates[0]
    expected = Decimal(str(expense.amount)).quantize(Decimal("0.01"))
    actual = Decimal(str(amount)).quantize(Decimal("0.01"))
    detail = {
        "match_method": match_method,
        "canonical_room_id": expense.room_id,
        "vendor_amount": str(actual),
        "system_amount": str(expected),
    }
    if expense.order_id:
        detail["canonical_order_id"] = expense.order_id
    if expected != actual:
        return "amount_mismatch", expense.expense_id, "amount_mismatch", detail
    return "matched", expense.expense_id, None, detail


async def process_service_document(
    db: AsyncSession,
    document: MonthlyCloseDocument,
    *,
    mapping: ServiceStatementMapping | None = None,
    billing_month: str | None = None,
) -> list[MonthlyCloseServiceLine]:
    """Parse one immutable document and replace only its derived rows."""
    try:
        if document.source_type == "cleaning_statement":
            from app.services.monthly_close.cleaning_work_log import parse_cleaning_work_log
            entries = parse_cleaning_work_log(document.content, document.filename, billing_month)
            if entries is not None:
                # A daily work log is successfully recognized activity evidence;
                # it has no supplier amounts to map into service invoice lines.
                await db.execute(delete(MonthlyCloseServiceLine).where(
                    MonthlyCloseServiceLine.document_id == document.document_id))
                document.processing_status = "stored"
                document.processing_error = None
                document.metadata_ = {
                    **(document.metadata_ or {}), "parser": "cleaning_work_log_v1",
                    "mapping_required": False,
                    "work_log_recognition": {
                        "source_sha256": document.sha256, "record_count": len(entries),
                        "normal_count": sum(e.service_type == "cleaning" for e in entries),
                        "instay_count": sum(e.service_type == "instay_cleaning" for e in entries),
                    },
                }
                await db.flush()
                return []
        parsed = parse_service_statement(
            document.content,
            document.filename,
            document.source_type,
            mapping=mapping,
            billing_month=billing_month,
        )
    except (ServiceStatementError, BillParseError) as exc:
        document.processing_status = "rejected"
        document.processing_error = str(exc)
        document.metadata_ = {
            **(document.metadata_ or {}),
            "parser": "service_statement_v1",
            "mapping_required": True,
        }
        return []

    await db.execute(
        delete(MonthlyCloseServiceLine).where(
            MonthlyCloseServiceLine.document_id == document.document_id
        )
    )
    categories = {
        category
        for line in parsed.lines
        if (category := _CATEGORY_BY_SERVICE_TYPE.get(line.service_type)) is not None
    }
    room_rows = list((await db.execute(select(Room.room_id, Room.room_name))).tuples())
    room_aliases = _room_alias_index(room_rows)
    resolved_rooms = [
        _resolve_room_reference(line.room_ref, room_aliases) for line in parsed.lines
    ]
    room_ids = {
        room_id for room_id, _, _ in resolved_rooms if room_id is not None
    }
    candidate_rows: list[tuple[Expense, str | None]] = []
    if categories and room_ids:
        candidate_rows = list(
            (
                await db.execute(
                    select(Expense, Order.platform_order_id)
                    .outerjoin(Order, Order.order_id == Expense.order_id)
                    .where(
                        Expense.is_deleted.is_(False),
                        Expense.is_service_fee.is_(True),
                        Expense.category.in_(categories),
                        Expense.room_id.in_(room_ids),
                    )
                )
            ).tuples()
        )
    by_order: dict[tuple[ExpenseCategory, str, str], list[Expense]] = defaultdict(list)
    by_date: dict[tuple[ExpenseCategory, str, object], list[Expense]] = defaultdict(list)
    for expense, platform_order_id in candidate_rows:
        if expense.room_id is None:
            continue
        if expense.order_id:
            order_aliases = {
                _reference_key(expense.order_id),
                _reference_key(platform_order_id),
            }
            for order_alias in order_aliases - {""}:
                by_order[(expense.category, expense.room_id, order_alias)].append(expense)
        by_date[(expense.category, expense.room_id, expense.expense_date)].append(expense)

    grouped_lines: dict[
        str, list[tuple[ServiceStatementLine, str | None, tuple[str, ...], str]]
    ] = defaultdict(list)
    for parsed_line, room_resolution in zip(parsed.lines, resolved_rooms, strict=True):
        canonical_room_id, room_candidates, room_match_method = room_resolution
        category = _CATEGORY_BY_SERVICE_TYPE.get(parsed_line.service_type)
        canonical_order_ref = _reference_key(parsed_line.order_ref)
        if category is not None and canonical_room_id and parsed_line.order_ref:
            order_candidates = by_order.get(
                (category, canonical_room_id, canonical_order_ref), []
            )
            canonical_order_ids = {
                expense.order_id for expense in order_candidates if expense.order_id
            }
            if len(canonical_order_ids) == 1:
                canonical_order_ref = next(iter(canonical_order_ids))
        grouping_key = "|".join(
            (
                parsed_line.service_type,
                canonical_order_ref,
                canonical_room_id or _reference_key(parsed_line.room_ref),
                parsed_line.service_date.isoformat(),
            )
        )
        grouped_lines[grouping_key].append(
            (parsed_line, canonical_room_id, room_candidates, room_match_method)
        )

    created: list[MonthlyCloseServiceLine] = []
    for grouping_key, grouped in grouped_lines.items():
        first_line, canonical_room_id, room_candidates, room_match_method = grouped[0]
        group_amount = sum(
            (line.amount for line, _, _, _ in grouped), Decimal("0.00")
        ).quantize(Decimal("0.01"))
        quantities = [line.quantity for line, _, _, _ in grouped]
        group_quantity = (
            sum((item for item in quantities if item is not None), Decimal("0"))
            .quantize(Decimal("0.001"))
            if all(item is not None for item in quantities)
            else None
        )
        unit_prices = {line.unit_price for line, _, _, _ in grouped}
        group_unit_price = (
            next(iter(unit_prices))
            if len(unit_prices) == 1 and None not in unit_prices
            else None
        )
        canonical_order_ref = _reference_key(first_line.order_ref)
        category = _CATEGORY_BY_SERVICE_TYPE.get(first_line.service_type)
        if category is not None and canonical_room_id and first_line.order_ref:
            order_ids = {
                candidate.order_id
                for candidate in by_order.get(
                    (
                        category,
                        canonical_room_id,
                        _reference_key(first_line.order_ref),
                    ),
                    [],
                )
                if candidate.order_id
            }
            if len(order_ids) == 1:
                canonical_order_ref = next(iter(order_ids))
        business_key = "|".join(
            (
                document.source_type,
                first_line.service_type,
                canonical_order_ref,
                canonical_room_id or _reference_key(first_line.room_ref),
                first_line.service_date.isoformat(),
                f"quantity={group_quantity if group_quantity is not None else '-'}",
                f"unit_price={group_unit_price if group_unit_price is not None else '-'}",
                f"amount={group_amount}",
            )
        )
        if canonical_room_id is None:
            match_status, expense_id, issue_code, match_detail = (
                "ambiguous",
                None,
                "ambiguous_room_reference",
                {
                    "match_method": room_match_method,
                    "candidate_room_ids": list(room_candidates),
                },
            )
        else:
            match_status, expense_id, issue_code, match_detail = _match_expense(
                service_type=first_line.service_type,
                order_ref=first_line.order_ref,
                room_ref=canonical_room_id,
                service_date=first_line.service_date,
                amount=group_amount,
                by_order=by_order,
                by_date=by_date,
            )
        group_detail = {
            **match_detail,
            "canonical_room_id": canonical_room_id,
            "group_line_count": len(grouped),
            "group_amount": str(group_amount),
        }
        for parsed_line, _, _, _ in grouped:
            row = MonthlyCloseServiceLine(
                line_id="MCS-" + uuid4().hex[:12].upper(),
                document_id=document.document_id,
                service_date=parsed_line.service_date,
                room_ref=parsed_line.room_ref,
                order_ref=parsed_line.order_ref,
                service_type=parsed_line.service_type,
                quantity=parsed_line.quantity,
                unit_price=parsed_line.unit_price,
                amount=parsed_line.amount,
                source_sheet=parsed_line.source_sheet,
                source_row_number=parsed_line.source_row_number,
                raw_values=parsed_line.raw_values,
                business_key=business_key,
                match_status=match_status,
                issue_code=issue_code,
                matched_expense_id=expense_id,
                match_detail={**group_detail, "line_amount": str(parsed_line.amount)},
            )
            db.add(row)
            created.append(row)
    await db.flush()

    business_keys = sorted(
        {line.business_key for line in created if line.business_key}
    )
    duplicate_rows: list[MonthlyCloseServiceLine] = []
    if business_keys:
        duplicate_rows = list(
            (
                await db.execute(
                    select(MonthlyCloseServiceLine)
                    .join(
                        MonthlyCloseDocument,
                        MonthlyCloseDocument.document_id
                        == MonthlyCloseServiceLine.document_id,
                    )
                    .where(
                        MonthlyCloseDocument.cycle_id == document.cycle_id,
                        MonthlyCloseDocument.is_active.is_(True),
                        MonthlyCloseServiceLine.business_key.in_(business_keys),
                    )
                )
            ).scalars()
        )
    duplicate_groups: dict[str, list[MonthlyCloseServiceLine]] = defaultdict(list)
    for line in duplicate_rows:
        if line.business_key:
            duplicate_groups[line.business_key].append(line)
    for business_key in business_keys:
        duplicates = duplicate_groups.get(business_key, [])
        duplicate_document_ids = sorted({item.document_id for item in duplicates})
        if len(duplicate_document_ids) > 1:
            duplicate_ids = sorted(item.line_id for item in duplicates)
            for item in duplicates:
                item.match_status = "duplicate"
                item.issue_code = "duplicate_business_key"
                item.matched_expense_id = None
                item.match_detail = {
                    **(item.match_detail or {}),
                    "duplicate_line_ids": duplicate_ids,
                    "duplicate_document_ids": duplicate_document_ids,
                }

    document.processing_status = "processed"
    document.processing_error = None
    document.metadata_ = {
        **(document.metadata_ or {}),
        "parser": "service_statement_v1",
        "mapping": {
            "sheet": parsed.mapping.sheet,
            "header_row": parsed.mapping.header_row,
            "columns": parsed.mapping.columns,
        },
        "line_count": len(created),
    }
    from app.services.monthly_close.layout_memory import remember_document_mapping

    remember_document_mapping(
        document,
        data=document.content,
        filename=document.filename,
        mapping=parsed.mapping,
    )
    return created
