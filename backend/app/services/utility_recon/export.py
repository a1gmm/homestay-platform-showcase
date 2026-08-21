from io import BytesIO

from openpyxl import Workbook
from openpyxl.styles import Font


SHEETS = ["简明结论", "原始楼层科目汇总", "修正后汇总", "逐日差异", "异常建议", "已排除记录", "口径说明"]


def _safe(value):
    if isinstance(value, str) and value[:1] in {"=", "+", "-", "@"}:
        return "'" + value
    return value


def _append(sheet, values):
    sheet.append([_safe(value) for value in values])


def _summary_sheet(sheet, summary):
    _append(sheet, ["楼层", "项目", "已收金额", "费用金额", "差额"])
    for item in summary.get("by_floor_category", []):
        _append(sheet, [item["floor"], "水费" if item["category"] == "water" else "电费", item["receipt"], item["expense"], item["difference"]])


def build_export(detail: dict) -> bytes:
    workbook = Workbook()
    workbook.remove(workbook.active)
    sheets = {name: workbook.create_sheet(name) for name in SHEETS}
    batch = detail["batch"]
    raw = batch["raw_summary"]
    corrected = batch["corrected_summary"]
    conclusion = sheets["简明结论"]
    _append(conclusion, ["统计月份", batch["month"]])
    _append(conclusion, ["已收金额", raw["receipt_total"]])
    _append(conclusion, ["费用金额", raw["expense_total"]])
    _append(conclusion, ["原始差额", raw["total_difference"]])
    _append(conclusion, ["修正后差额", corrected["total_difference"]])
    _summary_sheet(sheets["原始楼层科目汇总"], raw)
    _summary_sheet(sheets["修正后汇总"], corrected)
    daily = sheets["逐日差异"]
    _append(daily, ["来源文件", "工作表", "行号", "方向", "日期", "楼层", "房间", "项目", "金额"])
    for row in detail["rows"]:
        if row.get("disposition") == "valid":
            _append(daily, [row.get("source_filename"), row.get("source_sheet"), row.get("source_row_number"), row.get("side"), row.get("business_date"), row.get("floor"), row.get("room"), row.get("category"), row.get("amount")])
    suggestion_sheet = sheets["异常建议"]
    _append(suggestion_sheet, ["类型", "可信度", "状态", "证据", "建议修正"])
    for item in detail["suggestions"]:
        _append(suggestion_sheet, [item.get("kind"), item.get("confidence"), item.get("status"), str(item.get("evidence", {})), str(item.get("patch", {}))])
    excluded = sheets["已排除记录"]
    _append(excluded, ["来源文件", "工作表", "行号", "原因"])
    for row in detail["rows"]:
        if row.get("disposition") != "valid":
            _append(excluded, [row.get("source_filename"), row.get("source_sheet"), row.get("source_row_number"), row.get("exclusion_reason")])
    notes = sheets["口径说明"]
    _append(notes, ["差额口径", "已收金额 - 费用金额"])
    _append(notes, ["修正规则", "只应用人工采纳的楼层或科目建议；不修改原始 Excel"])
    for sheet in workbook:
        sheet.freeze_panes = "A2"
        for cell in sheet[1]:
            cell.font = Font(bold=True)
    output = BytesIO()
    workbook.save(output)
    return output.getvalue()
