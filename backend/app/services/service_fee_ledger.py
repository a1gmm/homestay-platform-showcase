"""Shared identity and transaction lock for owner service-fee ledger writers."""
from datetime import date
from types import MappingProxyType
from typing import Mapping

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.sql.elements import ColumnElement

from app.models.expense import Expense, ExpenseCategory
from app.models.owner import Owner


CHECKOUT_SERVICE_FEE_PREFIXES: Mapping[ExpenseCategory, str] = MappingProxyType(
    {
        ExpenseCategory.cleaning: "退房打扫",
        ExpenseCategory.laundry: "洗涤费",
        ExpenseCategory.daily_supplies: "日耗品",
    }
)


class CheckoutServiceFeeWrongMonthError(RuntimeError):
    """An active checkout-fee business key is posted outside its checkout month."""

    code = "checkout_service_fee_wrong_month"

    def __init__(
        self,
        *,
        order_id: str,
        room_id: str,
        expected_date: date,
        conflicts: tuple[tuple[ExpenseCategory, date | None], ...],
    ) -> None:
        self.order_id = order_id
        self.room_id = room_id
        self.expected_date = expected_date
        self.conflicts = conflicts
        super().__init__(
            "checkout service-fee ledger contains active keys outside "
            f"expected month order={order_id} room={room_id} "
            f"expected_month={expected_date:%Y-%m}"
        )

    def to_detail(self) -> dict:
        """Return a transport-safe conflict payload for API and card callers."""
        return {
            "code": self.code,
            "message": "退房服务费账本月份冲突，请财务核对后重试",
            "order_id": self.order_id,
            "room_id": self.room_id,
            "expected_month": self.expected_date.strftime("%Y-%m"),
            "conflicts": [
                {
                    "category": category.value,
                    "actual_month": (
                        expense_date.strftime("%Y-%m")
                        if expense_date is not None
                        else None
                    ),
                }
                for category, expense_date in self.conflicts
            ],
        }


def checkout_service_fee_identity_clause() -> ColumnElement[bool]:
    """Match checkout fees by category and canonical description origin.

    Prefix matching deliberately accepts suffixes such as room/night details and
    `（历史补录）`, while excluding a renewal cleaning whose origin is `续住打扫`.
    """
    return or_(
        *(
            and_(
                Expense.category == category,
                Expense.description.like(f"{prefix}%"),
            )
            for category, prefix in CHECKOUT_SERVICE_FEE_PREFIXES.items()
        )
    )


async def lock_owner_service_fee_ledger(db: AsyncSession, owner_id: str) -> None:
    """Serialize automatic service-fee writers for one owner until transaction end.

    Locking the stable owner row protects the empty-ledger case where there is no
    Expense row to lock yet.  The caller retains responsibility for commit/rollback.
    """
    await db.execute(
        select(Owner.owner_id)
        .where(Owner.owner_id == owner_id)
        .with_for_update()
    )
