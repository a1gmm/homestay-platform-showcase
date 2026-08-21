"""订单取消/作废时的关联费用收尾。"""
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expense import Expense, ExpensePayer


CENT = Decimal("0.01")


@dataclass(frozen=True)
class VoidedOwnerExpenses:
    count: int
    amount: Decimal
    expense_ids: tuple[str, ...]

    def response_summary(self) -> dict[str, int | str]:
        return {"count": self.count, "amount": str(self.amount.quantize(CENT))}


async def void_owner_expenses_for_order(
    db: AsyncSession, order_id: str, actor_id: str
) -> VoidedOwnerExpenses:
    """软删订单下仍有效的业主承担费用；公司已发生成本原样保留。

    不在此提交事务，调用方必须与取消/删除订单及审计记录一起提交。
    """
    expenses = (await db.execute(
        select(Expense).where(
            Expense.order_id == order_id,
            Expense.payer == ExpensePayer.owner,
            Expense.is_deleted.is_(False),
        )
    )).scalars().all()
    now = datetime.now(timezone.utc)
    total = Decimal("0")
    ids: list[str] = []
    for expense in expenses:
        expense.is_deleted = True
        expense.deleted_at = now
        expense.deleted_by = actor_id
        total += Decimal(expense.amount or 0)
        ids.append(expense.expense_id)
    return VoidedOwnerExpenses(
        count=len(ids),
        amount=total.quantize(CENT),
        expense_ids=tuple(sorted(ids)),
    )
