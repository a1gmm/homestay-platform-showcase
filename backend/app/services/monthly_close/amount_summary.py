"""Current expense-ledger totals; historical activity is never treated as money."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import func, select

from app.models.cleaning_work_record import CleaningWorkRecord
from app.models.expense import EXPENSE_CATEGORY_LABELS, Expense, ExpenseCategory


class ExpenseSubtotal(BaseModel):
    model_config = ConfigDict(extra="forbid")
    category: str
    label: str
    count: int
    amount: str


class AmountSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")
    booked_total: str
    booked_count: int
    all_expenses_total: str
    category_filter: list[str] = Field(default_factory=list)
    subtotals: list[ExpenseSubtotal] = Field(default_factory=list)
    historical_cleaning_quantity: int | None = None
    historical_without_same_day_fee: int | None = None
    historical_multiple_quantity_with_fee: int | None = None


CATEGORIES = {
    "cleaning_statement": {ExpenseCategory.cleaning},
    "linen_statement": {ExpenseCategory.laundry, ExpenseCategory.new_linen_prewash},
    "utility_expense": {
        ExpenseCategory.utilities, ExpenseCategory.public_utilities, ExpenseCategory.water,
        ExpenseCategory.cold_water, ExpenseCategory.hot_water, ExpenseCategory.electricity,
        ExpenseCategory.gas,
    },
    "operating_expenses": {
        ExpenseCategory.maintenance, ExpenseCategory.supplies, ExpenseCategory.daily_supplies,
        ExpenseCategory.broadband, ExpenseCategory.property_fee, ExpenseCategory.other,
        ExpenseCategory.property_guidance_fee, ExpenseCategory.tax,
    },
}
LABELS = {"all": "全部支出", "cleaning_statement": "保洁费用", "linen_statement": "布草洗涤费用",
          "utility_expense": "水电燃气费用", "operating_expenses": "其他运营支出"}


async def read_amount_summary(db, billing_month: str, focus: str, categories=None):
    year, month = map(int, billing_month.split("-"))
    start = date(year, month, 1)
    end = date(year + (month == 12), 1 if month == 12 else month + 1, 1)
    scope = (Expense.expense_date >= start, Expense.expense_date < end, Expense.is_deleted.is_(False))
    rows = (await db.execute(select(Expense.category, func.count(), func.sum(Expense.amount))
                            .where(*scope).group_by(Expense.category).order_by(Expense.category))).all()
    selected_categories = {ExpenseCategory(value) for value in categories} if categories else CATEGORIES.get(focus)
    selected = [row for row in rows if selected_categories is None or row[0] in selected_categories]
    total = sum((row[2] for row in selected), Decimal("0"))
    all_total = sum((row[2] for row in rows), Decimal("0"))
    count = sum(row[1] for row in selected)
    summary = AmountSummary(booked_total=f"{total:.2f}", booked_count=count,
                            all_expenses_total=f"{all_total:.2f}", category_filter=sorted(item.value for item in selected_categories) if selected_categories else [],
                            subtotals=[ExpenseSubtotal(category=category.value, label=EXPENSE_CATEGORY_LABELS[category],
                                count=n, amount=f"{amount:.2f}") for category, n, amount in selected])
    label = "、".join(EXPENSE_CATEGORY_LABELS[item] for item in sorted(selected_categories, key=lambda item: item.value)) if categories else LABELS[focus]
    message = f"{billing_month} 系统已入账的{label}合计 {total:,.2f} 元，共 {count} 笔。"
    if focus == "all":
        message += "\n" + "；".join(f"{row.label} {Decimal(row.amount):,.2f} 元" for row in summary.subtotals) + ("。" if selected else "当前账本没有该月有效支出。")
    else:
        message += f"\n这是财务页全部支出 {all_total:,.2f} 元中的一部分。"
    if selected_categories is None or ExpenseCategory.cleaning in selected_categories:
        records = (await db.scalars(select(CleaningWorkRecord).where(
            CleaningWorkRecord.service_date >= start, CleaningWorkRecord.service_date < end))).all()
        if records:
            fees = (await db.execute(select(Expense.room_id, Expense.expense_date, Expense.description)
                                    .where(*scope, Expense.category == ExpenseCategory.cleaning))).all()
            keys = {(room, day, "cleaning" if description.startswith("退房打扫") else "instay_cleaning")
                    for room, day, description in fees
                    if description.startswith(("退房打扫", "续住打扫"))}
            summary.historical_cleaning_quantity = sum(row.quantity for row in records)
            summary.historical_without_same_day_fee = sum(row.quantity for row in records
                if (row.room_id, row.service_date, row.service_type) not in keys)
            summary.historical_multiple_quantity_with_fee = sum(1 for row in records
                if row.quantity > 1 and (row.room_id, row.service_date, row.service_type) in keys)
            message += (f"\n\n本月补录了 {summary.historical_cleaning_quantity} 次历史打扫记录；"
                        "补录记录不会自动生成费用，已入账金额不能代表原表全部打扫的最终费用。")
            if summary.historical_without_same_day_fee:
                message += (f"按同房、同日、打扫类型初筛，其中 {summary.historical_without_same_day_fee} 次未找到明确对应的标准保洁费用。"
                            "还需核对关联订单、跨日记账和手工费用，不能直接把这些次数全部新增收费。")
            else:
                message += "已有同房同日费用也需要逐笔核对次数与金额，不能直接认定费用已对齐。"
            if summary.historical_multiple_quantity_with_fee:
                message += (f"另有 {summary.historical_multiple_quantity_with_fee} 组同日多次打扫已有费用，"
                            "还需确认是否按实际次数收费。")
    message += "\n\n以上按支出发生日统计，排除已作废费用；仅查询账本，没有补记或修改费用。"
    return summary, message
