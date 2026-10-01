"""Persist explicit source-row evidence for operating-expense economic events."""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models.monthly_close import MonthlyCloseCycle, MonthlyCloseDocument
from app.models.room import Room
from app.services.monthly_close.documents import MonthlyCloseDocumentError
from app.services.monthly_close.operating_expenses import (
    OperatingExpenseMapping,
    build_room_aliases,
    expense_id_for_business_key,
    parse_operating_expense_workbook,
)


def normalized_operating_sheet(sheet: str) -> str:
    return re.sub(r"\s+", "", sheet).casefold()


def operating_source_slot(sheet: str, row_number: int) -> str:
    return f"{normalized_operating_sheet(sheet)}|{row_number}"


def operating_document_event_key(
    billing_month: str,
    document: MonthlyCloseDocument,
    *,
    sheet: str,
    row_number: int,
) -> str:
    lineage = str(
        (document.metadata_ or {}).get("operating_event_lineage")
        or document.document_id
    )
    return "|".join(
        (
            billing_month,
            "document-lineage",
            lineage,
            normalized_operating_sheet(sheet),
            str(row_number),
        )
    )


def _stored_mapping(document: MonthlyCloseDocument) -> OperatingExpenseMapping:
    stored = (document.metadata_ or {}).get("mapping")
    if not isinstance(stored, dict):
        raise MonthlyCloseDocumentError(
            "operating_expense_predecessor_mapping_unavailable",
            "前版运营支出原件尚未确认表格结构。",
            422,
        )
    try:
        return OperatingExpenseMapping(
            sheet=stored["sheet"],
            header_row=stored["header_row"],
            columns=stored["columns"],
            category_values=stored.get("category_values", {}),
            payer_values=stored.get("payer_values", {}),
        )
    except (KeyError, TypeError) as exc:
        raise MonthlyCloseDocumentError(
            "operating_expense_predecessor_mapping_unavailable",
            "前版运营支出表格结构证据不完整。",
            422,
        ) from exc


async def _parsed_rows(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    document: MonthlyCloseDocument,
    mapping: OperatingExpenseMapping,
):
    room_rows = list((await db.execute(select(Room.room_id, Room.room_name))).tuples())
    return parse_operating_expense_workbook(
        document.content,
        document.filename,
        cycle.billing_month,
        valid_room_ids={room_id for room_id, _room_name in room_rows},
        room_aliases=build_room_aliases(room_rows),
        mapping=mapping,
    )


async def _predecessors(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    document: MonthlyCloseDocument,
) -> list[MonthlyCloseDocument]:
    predecessor_ids = sorted(
        item
        for item in (document.metadata_ or {}).get("supersedes_document_ids", [])
        if isinstance(item, str)
    )
    if not predecessor_ids:
        return []
    rows = list(
        await db.scalars(
            select(MonthlyCloseDocument)
            .options(undefer(MonthlyCloseDocument.content))
            .where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.source_type == "operating_expenses",
                MonthlyCloseDocument.document_id.in_(predecessor_ids),
            )
            .order_by(MonthlyCloseDocument.document_id)
        )
    )
    if [item.document_id for item in rows] != predecessor_ids:
        raise MonthlyCloseDocumentError(
            "operating_expense_predecessor_not_found",
            "替代关系中的前版运营支出原件不存在。",
            422,
        )
    return rows


async def operating_replacement_context(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    document: MonthlyCloseDocument,
    mapping: OperatingExpenseMapping,
) -> dict[str, Any] | None:
    predecessors = await _predecessors(db, cycle, document)
    if not predecessors:
        return None
    current = await _parsed_rows(db, cycle, document, mapping)
    predecessor_rows: list[dict[str, Any]] = []
    for predecessor in predecessors:
        parsed = await _parsed_rows(db, cycle, predecessor, _stored_mapping(predecessor))
        predecessor_rows.extend(
            {
                "document_id": predecessor.document_id,
                "row_number": row.row_number,
                "source_sheet": parsed.mapping.sheet,
            }
            for row in parsed.rows
            if row.external_reference is None
        )
    return {
        "current_unreferenced_rows": sorted(
            row.row_number for row in current.rows if row.external_reference is None
        ),
        "predecessor_rows": sorted(
            predecessor_rows,
            key=lambda item: (
                item["document_id"],
                item["source_sheet"],
                item["row_number"],
            ),
        ),
    }


async def operating_predecessor_facts(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    document: MonthlyCloseDocument,
) -> list[tuple[MonthlyCloseDocument, Any]]:
    """Reload immutable predecessor rows even after the old document is archived."""
    predecessors = await _predecessors(db, cycle, document)
    return [
        (
            predecessor,
            await _parsed_rows(
                db,
                cycle,
                predecessor,
                _stored_mapping(predecessor),
            ),
        )
        for predecessor in predecessors
    ]


async def confirmed_operating_event_metadata(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    document: MonthlyCloseDocument,
    mapping: OperatingExpenseMapping,
    predecessor_rows: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    """Validate employee row coordinates and derive server-owned event identities."""
    current = await _parsed_rows(db, cycle, document, mapping)
    predecessors = await _predecessors(db, cycle, document)
    parsed_predecessors = {
        predecessor.document_id: (
            predecessor,
            await _parsed_rows(db, cycle, predecessor, _stored_mapping(predecessor)),
        )
        for predecessor in predecessors
    }
    current_by_number = {row.row_number: row for row in current.rows}
    selected_numbers = {int(value) for value in predecessor_rows}
    if not selected_numbers <= set(current_by_number):
        raise MonthlyCloseDocumentError(
            "operating_expense_predecessor_mapping_invalid",
            "前版行映射引用了当前原件中不存在的行。",
            422,
        )

    persisted: dict[str, dict[str, Any]] = {}
    links: dict[str, dict[str, Any]] = {}
    for row in current.rows:
        slot = operating_source_slot(current.mapping.sheet, row.row_number)
        if row.external_reference:
            event_key = (
                f"{cycle.billing_month}|external-reference|{row.external_reference}"
            )
        else:
            same_slot = next(
                (
                    (predecessor, parsed, predecessor_row)
                    for predecessor, parsed in parsed_predecessors.values()
                    for predecessor_row in parsed.rows
                    if predecessor_row.external_reference is None
                    and predecessor_row.row_number == row.row_number
                    and normalized_operating_sheet(parsed.mapping.sheet)
                    == normalized_operating_sheet(current.mapping.sheet)
                ),
                None,
            )
            selection = predecessor_rows.get(str(row.row_number))
            if selection is None and predecessors and same_slot is None:
                raise MonthlyCloseDocumentError(
                    "operating_expense_predecessor_mapping_required",
                    f"第{row.row_number}行在替代原件中移动，请选择对应的前版行。",
                    422,
                )
            if selection is not None:
                predecessor_id = str(selection.document_id)
                predecessor_row_number = int(selection.row_number)
                predecessor_entry = parsed_predecessors.get(predecessor_id)
                predecessor_row = (
                    next(
                        (
                            item
                            for item in predecessor_entry[1].rows
                            if item.row_number == predecessor_row_number
                            and item.external_reference is None
                        ),
                        None,
                    )
                    if predecessor_entry is not None
                    else None
                )
                if predecessor_entry is None or predecessor_row is None:
                    raise MonthlyCloseDocumentError(
                        "operating_expense_predecessor_mapping_invalid",
                        f"第{row.row_number}行选择的前版行不存在。",
                        422,
                    )
                predecessor, parsed = predecessor_entry
                event_key = operating_document_event_key(
                    cycle.billing_month,
                    predecessor,
                    sheet=parsed.mapping.sheet,
                    row_number=predecessor_row_number,
                )
                persisted[slot] = {
                    "predecessor_document_id": predecessor_id,
                    "predecessor_event_key": event_key,
                    "predecessor_row_number": predecessor_row_number,
                }
            elif same_slot is not None:
                predecessor, parsed, predecessor_row = same_slot
                event_key = operating_document_event_key(
                    cycle.billing_month,
                    predecessor,
                    sheet=parsed.mapping.sheet,
                    row_number=predecessor_row.row_number,
                )
            else:
                event_key = operating_document_event_key(
                    cycle.billing_month,
                    document,
                    sheet=current.mapping.sheet,
                    row_number=row.row_number,
                )
        links[slot] = {
            "event_key": event_key,
            "expense_id": expense_id_for_business_key(event_key),
            "source_row_number": row.row_number,
            "source_sheet": current.mapping.sheet,
        }
    return persisted, links
