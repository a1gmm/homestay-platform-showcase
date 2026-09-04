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


class CheckoutExpenseRecognitionError(RuntimeError):
    """Checkout cannot recognize every incurred service expense safely."""

    code = "checkout_expense_recognition_failed"
    _MESSAGES = MappingProxyType(
        {
            "missing_order": "订单不存在，无法记录退房支出",
            "no_rooms": "订单没有已分配房间，无法记录退房支出",
            "room_not_in_order": "本次退房房间不属于该订单，无法记录退房支出",
            "missing_room": "房间资料不存在，无法记录退房支出",
            "missing_owner": "房间未绑定业主，无法记录退房支出",
            "missing_checkout_date": "无法确定最终退房日期，无法记录退房支出",
        }
    )

    def __init__(
        self,
        *,
        reason: str,
        order_id: str | None,
        room_ids: tuple[str, ...] = (),
    ) -> None:
        self.reason = reason
        self.order_id = order_id
        self.room_ids = room_ids
        super().__init__(
            f"checkout expense recognition failed reason={reason} "
            f"order={order_id} rooms={','.join(room_ids)}"
        )

    def to_detail(self) -> dict:
        """Return a stable, actionable payload for checkout API callers."""
        return {
            "code": self.code,
            "message": self._MESSAGES.get(
                self.reason,
                "退房支出记录失败，请核对订单与房间资料后重试",
            ),
            "reason": self.reason,
            "order_id": self.order_id,
            "room_ids": list(self.room_ids),
        }


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
