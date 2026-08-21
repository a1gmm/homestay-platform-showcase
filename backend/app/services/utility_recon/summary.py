from dataclasses import dataclass
from decimal import Decimal

from .contracts import NormalizedRow


ZERO = Decimal("0.00")


@dataclass(frozen=True)
class AmountSummary:
    receipt: Decimal
    expense: Decimal
    difference: Decimal


@dataclass(frozen=True)
class ReconciliationSummary:
    by_floor_category: dict[tuple[str, str], AmountSummary]
    receipt_total: Decimal
    expense_total: Decimal
    total_difference: Decimal


def build_summary(rows: list[NormalizedRow]) -> ReconciliationSummary:
    totals: dict[tuple[str, str], list[Decimal]] = {}
    receipt_total = ZERO
    expense_total = ZERO
    for row in rows:
        if row.disposition != "valid" or row.amount is None or not row.floor or not row.category:
            continue
        key = (row.floor, row.category)
        pair = totals.setdefault(key, [ZERO, ZERO])
        if row.side == "receipt":
            pair[0] += row.amount
            receipt_total += row.amount
        else:
            pair[1] += row.amount
            expense_total += row.amount
    floors = {row.floor for row in rows if row.floor and row.disposition == "valid"}
    for floor in floors:
        totals.setdefault((floor, "water"), [ZERO, ZERO])
        totals.setdefault((floor, "electricity"), [ZERO, ZERO])
    summaries = {
        key: AmountSummary(receipt.quantize(Decimal("0.01")), expense.quantize(Decimal("0.01")), (receipt - expense).quantize(Decimal("0.01")))
        for key, (receipt, expense) in totals.items()
    }
    return ReconciliationSummary(
        summaries,
        receipt_total.quantize(Decimal("0.01")),
        expense_total.quantize(Decimal("0.01")),
        (receipt_total - expense_total).quantize(Decimal("0.01")),
    )


def build_conclusion(month: str, summary: ReconciliationSummary) -> str:
    direction = "已收更多" if summary.total_difference >= 0 else "费用更多"
    return (
        f"{month} 已收 ¥{summary.receipt_total:.2f}，费用 ¥{summary.expense_total:.2f}，"
        f"差额 ¥{abs(summary.total_difference):.2f}（{direction}）。"
    )
