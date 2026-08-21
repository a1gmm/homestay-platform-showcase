"""确定性匹配和受控字段修正。"""

from dataclasses import dataclass, replace
from itertools import combinations
from typing import Any

from .contracts import NormalizedRow
from .summary import ReconciliationSummary, build_summary


@dataclass(frozen=True)
class Suggestion:
    kind: str
    related_row_ids: tuple[str, ...]
    patch: dict[str, Any]
    evidence: dict[str, Any]
    confidence: str


@dataclass(frozen=True)
class ReconciliationResult:
    month: str
    rows: list[NormalizedRow]
    raw: ReconciliationSummary
    suggestions: list[Suggestion]


def _same_exact(left: NormalizedRow, right: NormalizedRow) -> bool:
    room_matches = not left.room or not right.room or left.room == right.room
    return (
        left.business_date == right.business_date
        and left.floor == right.floor
        and left.category == right.category
        and left.amount == right.amount
        and room_matches
    )


def reconcile_month(month: str, rows: list[NormalizedRow]) -> ReconciliationResult:
    valid = [row for row in rows if row.disposition == "valid" and row.month == month]
    receipts = [row for row in valid if row.side == "receipt"]
    expenses = [row for row in valid if row.side == "expense"]
    consumed_receipts: set[str] = set()
    consumed_expenses: set[str] = set()

    # 房号信息完整的记录优先；随后允许至少一方没有房号的精确匹配。
    for require_room in (True, False):
        for receipt in receipts:
            if receipt.row_id in consumed_receipts:
                continue
            for expense in expenses:
                if expense.row_id in consumed_expenses:
                    continue
                if require_room and (not receipt.room or not expense.room):
                    continue
                if not require_room and receipt.room and expense.room:
                    continue
                if _same_exact(receipt, expense):
                    consumed_receipts.add(receipt.row_id)
                    consumed_expenses.add(expense.row_id)
                    break

    suggestions: list[Suggestion] = []
    # 同楼层/科目/金额，付款日晚 1–7 天。
    for receipt in receipts:
        if receipt.row_id in consumed_receipts or not receipt.business_date:
            continue
        for expense in expenses:
            if expense.row_id in consumed_expenses or not expense.business_date:
                continue
            delay = (expense.business_date - receipt.business_date).days
            if (
                1 <= delay <= 7
                and receipt.floor == expense.floor
                and receipt.category == expense.category
                and receipt.amount == expense.amount
            ):
                suggestions.append(Suggestion(
                    "delayed_payment", (receipt.row_id, expense.row_id), {},
                    {"delay_days": delay, "amount": str(receipt.amount)}, "high",
                ))
                consumed_receipts.add(receipt.row_id)
                consumed_expenses.add(expense.row_id)
                break

    # 一笔费用对应 2–10 笔同楼层/科目、七天窗口内的已收合计。
    remaining_receipts = [row for row in receipts if row.row_id not in consumed_receipts]
    for expense in expenses:
        if expense.row_id in consumed_expenses or not expense.business_date or expense.amount is None:
            continue
        candidates = [
            row for row in remaining_receipts
            if row.row_id not in consumed_receipts
            and row.floor == expense.floor and row.category == expense.category
            and row.business_date and 0 <= (expense.business_date - row.business_date).days <= 7
        ][:10]
        matched = None
        for size in range(2, len(candidates) + 1):
            for group in combinations(candidates, size):
                if sum((row.amount for row in group if row.amount is not None), start=0) == expense.amount:
                    matched = group
                    break
            if matched:
                break
        if matched:
            row_ids = tuple(row.row_id for row in matched) + (expense.row_id,)
            suggestions.append(Suggestion(
                "merged_payment", row_ids, {}, {"amount": str(expense.amount), "receipt_count": len(matched)}, "high"
            ))
            consumed_receipts.update(row.row_id for row in matched)
            consumed_expenses.add(expense.row_id)

    for receipt in receipts:
        if receipt.row_id in consumed_receipts:
            continue
        for expense in expenses:
            if expense.row_id in consumed_expenses:
                continue
            if (
                receipt.business_date == expense.business_date
                and receipt.floor == expense.floor
                and receipt.amount == expense.amount
                and receipt.category != expense.category
            ):
                suggestions.append(Suggestion(
                    kind="category_mismatch",
                    related_row_ids=(receipt.row_id, expense.row_id),
                    patch={"row_id": expense.row_id, "category": receipt.category},
                    evidence={"date": str(receipt.business_date), "floor": receipt.floor, "amount": str(receipt.amount)},
                    confidence="high",
                ))
                consumed_receipts.add(receipt.row_id)
                consumed_expenses.add(expense.row_id)
                break
    # 同日、同科目、同金额却落在不同楼层，作为楼层误写候选。
    for receipt in receipts:
        if receipt.row_id in consumed_receipts:
            continue
        for expense in expenses:
            if expense.row_id in consumed_expenses:
                continue
            if (
                receipt.business_date == expense.business_date
                and receipt.category == expense.category
                and receipt.amount == expense.amount
                and receipt.floor != expense.floor
            ):
                suggestions.append(Suggestion(
                    "floor_mismatch", (receipt.row_id, expense.row_id),
                    {"row_id": expense.row_id, "floor": receipt.floor},
                    {"date": str(receipt.business_date), "amount": str(receipt.amount), "receipt_floor": receipt.floor, "expense_floor": expense.floor},
                    "medium",
                ))
                consumed_receipts.add(receipt.row_id)
                consumed_expenses.add(expense.row_id)
                break
    for receipt in receipts:
        if receipt.row_id not in consumed_receipts:
            suggestions.append(Suggestion(
                "receipt_only", (receipt.row_id,), {}, {"amount": str(receipt.amount), "floor": receipt.floor, "category": receipt.category}, "low"
            ))
    for expense in expenses:
        if expense.row_id not in consumed_expenses:
            suggestions.append(Suggestion(
                "expense_only", (expense.row_id,), {}, {"amount": str(expense.amount), "floor": expense.floor, "category": expense.category}, "low"
            ))
    return ReconciliationResult(month, valid, build_summary(valid), suggestions)


def apply_patches(result: ReconciliationResult, patches: list[dict[str, Any]]) -> ReconciliationSummary:
    allowed = {"row_id", "floor", "category"}
    by_id = {row.row_id: row for row in result.rows}
    for patch in patches:
        if not set(patch) <= allowed or "row_id" not in patch:
            raise ValueError("修正建议包含不允许的字段")
        row_id = str(patch["row_id"])
        row = by_id.get(row_id)
        if row is None:
            raise ValueError("修正建议引用的来源行不存在")
        changes = {key: patch[key] for key in ("floor", "category") if key in patch}
        by_id[row_id] = replace(row, **changes)
    return build_summary(list(by_id.values()))
