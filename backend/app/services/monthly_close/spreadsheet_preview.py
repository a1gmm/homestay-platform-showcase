"""Safe, administrator-facing workbook structure preview."""

from __future__ import annotations

from app.services.billing_recon.parser import BillParseError, load_workbook_rows
from app.services.monthly_close.documents import MonthlyCloseDocumentError
from app.services.spreadsheet_ai_sample import build_safe_workbook_structure


def build_workbook_preview(data: bytes, filename: str) -> list[dict]:
    try:
        sheets, _ = load_workbook_rows(data, filename)
    except BillParseError as exc:
        raise MonthlyCloseDocumentError(
            "invalid_document", "请上传有效的 xls/xlsx 文件", 422
        ) from exc
    structure = build_safe_workbook_structure(sheets)
    token_by_sheet = {sheet: token for token, sheet in structure.sheet_tokens.items()}
    return [
        {
            "name": sheet_name,
            "row_count": len(rows),
            "column_count": max((len(row) for row in rows), default=0),
            "rows": structure.rows_by_token[token_by_sheet[sheet_name]],
        }
        for sheet_name, rows in sheets.items()
    ]
