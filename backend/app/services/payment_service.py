"""收款金额聚合的单一真相源。

房费收齐口径：只统计非押金（is_deposit == False）、未删除（is_deleted == False）的
收款 (#48)。此前该查询在 finance.py / orders.py 散落 8 处，口径一处改易漏；统一到本
helper（#97）。
"""
from decimal import Decimal

from sqlalchemy import select, func

from app.models.payment import Payment
from app.models.refund import Refund, RefundReason


async def sum_house_fee_paid(db, order_id: str, exclude_payment_id: str | None = None) -> Decimal:
    """订单已收房费合计（排除押金、已删除收款）。

    exclude_payment_id: 改某笔收款金额前，算「其它收款之和」用（update_payment 的上限校验）。
    """
    q = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.order_id == order_id,
        Payment.is_deleted == False,
        Payment.is_deposit == False,
    )
    if exclude_payment_id is not None:
        q = q.where(Payment.payment_id != exclude_payment_id)
    return (await db.scalar(q)) or Decimal("0")


async def sum_house_fee_refunded(db, order_id: str) -> Decimal:
    """订单已退房费合计（排除押金退还口径 deposit_return、已软删退款）。

    与 create_refund 的 prev_refunded 口径一致：押金退还走 deposit_status 工作流，
    不占房费退款额度。给收款 update/delete 的对称下限校验用（批2 item1）——
    改后房费实收不得低于本值，否则账面变负。
    """
    q = select(func.coalesce(func.sum(Refund.amount), 0)).where(
        Refund.order_id == order_id,
        Refund.reason != RefundReason.deposit_return,
        Refund.is_deleted == False,
    )
    return (await db.scalar(q)) or Decimal("0")


async def sum_deposit_paid(db, order_id: str, exclude_payment_id: str | None = None) -> Decimal:
    """订单已收押金合计（is_deposit=True、未软删）。给押金收款上限校验用（批2 item2）。"""
    q = select(func.coalesce(func.sum(Payment.amount), 0)).where(
        Payment.order_id == order_id,
        Payment.is_deleted == False,
        Payment.is_deposit == True,
    )
    if exclude_payment_id is not None:
        q = q.where(Payment.payment_id != exclude_payment_id)
    return (await db.scalar(q)) or Decimal("0")
