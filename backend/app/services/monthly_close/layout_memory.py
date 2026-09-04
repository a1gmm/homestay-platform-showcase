"""Reuse locally validated monthly-close mappings across billing months.

The signature contains only workbook structure and header positions. Confirmed
coordinates stay attached to the immutable source document, avoiding another
table while still allowing later cycles to reuse them.
"""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json
import re
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monthly_close import MonthlyCloseDocument, MonthlyCloseInboxItem
from app.services.audit import log_action_tx
from app.services.billing_recon.parser import BillParseError, load_workbook_rows
from app.services.monthly_close.documents import MonthlyCloseDocumentError
from app.services.spreadsheet_ai_sample import structural_cell_type


_SPACE_RE = re.compile(r"\s+")


def _sheets(data: bytes, filename: str) -> dict[str, list[list]]:
    try:
        sheets, _ = load_workbook_rows(data, filename)
        return sheets
    except BillParseError as exc:
        raise MonthlyCloseDocumentError(
            "invalid_document", "请上传有效的 xls/xlsx 文件", 422
        ) from exc


def _header_label(value: object) -> str:
    return _SPACE_RE.sub("", str(value or "").strip().casefold())[:80]


def _structural_payload(sheets: dict[str, list[list]]) -> str:
    structural_sheets: list[dict[str, Any]] = []
    for rows in sheets.values():
        headers: list[list[Any]] = []
        data_patterns: set[tuple[str, ...]] = set()
        for row_index, row in enumerate(rows[:30]):
            shapes = [structural_cell_type(value) for value in row[:100]]
            nonempty = [shape for shape in shapes if shape != "EMPTY"]
            following = rows[row_index + 1 : row_index + 4]
            following_has_data = any(
                "DATE" in {structural_cell_type(value) for value in next_row}
                and "NUMBER" in {structural_cell_type(value) for value in next_row}
                for next_row in following
            )
            if len(nonempty) >= 2 and set(nonempty) == {"TEXT"} and following_has_data:
                headers.append(
                    [
                        row_index,
                        [
                            [column_index, _header_label(value)]
                            for column_index, value in enumerate(row[:100])
                            if str(value or "").strip()
                        ],
                    ]
                )
            elif "DATE" in shapes and "NUMBER" in shapes:
                data_patterns.add(tuple(shapes))
        structural_sheets.append(
            {
                "columns": max((len(row) for row in rows[:30]), default=0),
                "headers": headers,
                "data_patterns": [list(pattern) for pattern in sorted(data_patterns)],
            }
        )
    return json.dumps(
        {"version": 1, "sheets": structural_sheets},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def workbook_layout_signature(data: bytes, filename: str) -> str:
    return sha256(_structural_payload(_sheets(data, filename)).encode("utf-8")).hexdigest()


def remember_document_mapping(
    document: MonthlyCloseDocument,
    *,
    data: bytes,
    filename: str,
    mapping: Any,
    user_id: str | None = None,
) -> None:
    sheets = _sheets(data, filename)
    names = list(sheets)
    try:
        sheet_index = names.index(mapping.sheet)
    except (ValueError, AttributeError) as exc:
        raise MonthlyCloseDocumentError(
            "mapping_sheet_missing", "确认的工作表不存在", 422
        ) from exc
    previous = (document.metadata_ or {}).get("layout_memory")
    previous = previous if isinstance(previous, dict) else {}
    document.metadata_ = {
        **(document.metadata_ or {}),
        "layout_memory": {
            "version": 1,
            "signature": sha256(_structural_payload(sheets).encode("utf-8")).hexdigest(),
            "sheet_index": sheet_index,
            "header_row": int(mapping.header_row),
            "columns": dict(mapping.columns),
            "category_values": dict(getattr(mapping, "category_values", {})),
            "payer_values": dict(getattr(mapping, "payer_values", {})),
            **({"role": mapping.role} if hasattr(mapping, "role") else {}),
            "enabled": bool(previous.get("enabled", True)),
            "approved_by": user_id or document.uploaded_by,
            "approved_at": previous.get("approved_at")
            or datetime.now(timezone.utc).isoformat(),
            "use_count": int(previous.get("use_count", 0)),
            "last_used_at": previous.get("last_used_at"),
        },
    }


async def find_remembered_mapping(
    db: AsyncSession,
    *,
    source_type: str,
    data: bytes,
    filename: str,
) -> dict[str, Any] | None:
    sheets = _sheets(data, filename)
    names = list(sheets)
    signature = sha256(_structural_payload(sheets).encode("utf-8")).hexdigest()
    documents = list(
        (
            await db.execute(
                select(MonthlyCloseDocument)
                .where(
                    MonthlyCloseDocument.source_type == source_type,
                    MonthlyCloseDocument.processing_status == "processed",
                )
                .order_by(MonthlyCloseDocument.uploaded_at.desc())
                .limit(100)
            )
        ).scalars()
    )
    for document in documents:
        memory = (document.metadata_ or {}).get("layout_memory")
        if not isinstance(memory, dict) or memory.get("signature") != signature:
            continue
        if memory.get("enabled", True) is False:
            continue
        sheet_index = memory.get("sheet_index")
        header_row = memory.get("header_row")
        columns = memory.get("columns")
        if (
            isinstance(sheet_index, int)
            and 0 <= sheet_index < len(names)
            and isinstance(header_row, int)
            and isinstance(columns, dict)
        ):
            result = {
                "sheet": names[sheet_index],
                "header_row": header_row,
                "columns": dict(columns),
            }
            if isinstance(memory.get("category_values"), dict) and memory["category_values"]:
                result["category_values"] = dict(memory["category_values"])
            if isinstance(memory.get("payer_values"), dict) and memory["payer_values"]:
                result["payer_values"] = dict(memory["payer_values"])
            if memory.get("role") in {"receipt", "expense"}:
                result["role"] = memory["role"]
            updated_memory = {
                **memory,
                "use_count": int(memory.get("use_count", 0)) + 1,
                "last_used_at": datetime.now(timezone.utc).isoformat(),
            }
            document.metadata_ = {
                **(document.metadata_ or {}),
                "layout_memory": updated_memory,
            }
            await db.flush()
            return result
    return None


def _memory_view(document: MonthlyCloseDocument, memory: dict[str, Any]) -> dict[str, Any]:
    mapping = {
        "sheet_index": memory.get("sheet_index"),
        "header_row": memory.get("header_row"),
        "columns": dict(memory.get("columns") or {}),
        "category_values": dict(memory.get("category_values") or {}),
        "payer_values": dict(memory.get("payer_values") or {}),
        **({"role": memory["role"]} if memory.get("role") else {}),
    }
    return {
        "document_id": document.document_id,
        "source_type": document.source_type,
        "filename": document.filename,
        "signature": memory.get("signature"),
        "version": int(memory.get("version", 1)),
        "mapping": mapping,
        "enabled": bool(memory.get("enabled", True)),
        "approved_by": memory.get("approved_by") or document.uploaded_by,
        "approved_at": memory.get("approved_at")
        or (document.uploaded_at.isoformat() if document.uploaded_at else None),
        "use_count": int(memory.get("use_count", 0)),
        "last_used_at": memory.get("last_used_at"),
        "disabled_by": memory.get("disabled_by"),
        "disabled_at": memory.get("disabled_at"),
    }


async def list_layout_memories(db: AsyncSession) -> list[dict[str, Any]]:
    documents = list(
        (
            await db.execute(
                select(MonthlyCloseDocument)
                .where(MonthlyCloseDocument.processing_status == "processed")
                .order_by(MonthlyCloseDocument.uploaded_at.desc())
                .limit(500)
            )
        ).scalars()
    )
    rows: list[dict[str, Any]] = []
    for document in documents:
        memory = (document.metadata_ or {}).get("layout_memory")
        if isinstance(memory, dict):
            rows.append(_memory_view(document, memory))
    return rows


async def set_layout_memory_enabled(
    db: AsyncSession,
    document_id: str,
    *,
    enabled: bool,
    user_id: str,
) -> dict[str, Any]:
    document = (
        await db.execute(
            select(MonthlyCloseDocument)
            .where(MonthlyCloseDocument.document_id == document_id)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if document is None:
        raise MonthlyCloseDocumentError("layout_memory_not_found", "布局记忆不存在", 404)
    memory = (document.metadata_ or {}).get("layout_memory")
    if not isinstance(memory, dict):
        raise MonthlyCloseDocumentError("layout_memory_not_found", "布局记忆不存在", 404)
    now = datetime.now(timezone.utc).isoformat()
    updated = {
        **memory,
        "enabled": enabled,
        "disabled_by": None if enabled else user_id,
        "disabled_at": None if enabled else now,
    }
    document.metadata_ = {**(document.metadata_ or {}), "layout_memory": updated}
    await log_action_tx(
        db,
        user_id,
        "monthly_close.layout_memory.enable" if enabled else "monthly_close.layout_memory.disable",
        "monthly_close_document",
        document.document_id,
        before_data={"enabled": bool(memory.get("enabled", True))},
        after_data={"enabled": enabled},
    )
    await db.commit()
    await db.refresh(document)
    return _memory_view(document, updated)


async def layout_memory_metrics(db: AsyncSession) -> dict[str, Any]:
    memories = await list_layout_memories(db)
    inbox_items = list((await db.execute(select(MonthlyCloseInboxItem))).scalars())
    document_count = len(
        list((await db.execute(select(MonthlyCloseDocument.document_id))).scalars())
    )
    active = sum(item["enabled"] for item in memories)
    reuse_count = sum(int(item["use_count"]) for item in memories)
    confirmations = len(memories)
    deterministic = sum(
        item.suggested_by in {"deterministic", "external_link"}
        for item in inbox_items
    )
    ai_suggested = sum(item.suggested_by == "ai" for item in inbox_items)
    administrator_corrected = sum(
        item.suggested_by == "administrator" for item in inbox_items
    )
    classified = deterministic + ai_suggested + administrator_corrected
    needs_confirmation = sum(
        item.status not in {"confirmed", "dismissed"} for item in inbox_items
    )
    return {
        "memory_count": confirmations,
        "active_memory_count": active,
        "disabled_memory_count": confirmations - active,
        "reuse_count": reuse_count,
        "file_count": document_count
        + sum(item.document_id is None for item in inbox_items),
        "deterministic_recognition_count": deterministic,
        "ai_suggestion_count": ai_suggested,
        "remembered_layout_count": reuse_count,
        "administrator_correction_count": administrator_corrected,
        "needs_confirmation_count": needs_confirmation,
        "automation_rate": round(
            (deterministic + ai_suggested) / classified * 100, 1
        )
        if classified
        else 0.0,
    }
