"""orders add deposit_returned + reorder workflow (确认收款挪到退房后)

Revision ID: f5b9c2d7e3a1
Revises: e3f4a5b6c7d8
Create Date: 2026-06-05 00:00:00.000000

王总 2026-06-05 反馈：订单流程「先确认收款再办理入住」不合适——房费常是入住后/
离店第二天（平台代收离店后才结算）才到账。把「确认收款」从下单后第 2 步挪到客人
退房之后、完成订单之前。新顺序：确认订单 → 确认排房 → 办理入住 → 发起退房 →
确认收款 → 完成订单。

本迁移做两件事：
1) orders 加列 deposit_returned（押金实退金额，可少于 deposit，差额即扣款）。
2) 数据迁移：把存量「早期 pending_payment」订单迁到 paid_pending_room（待排房）。
   因为 pending_payment 枚举值被重定义为后期「待完成」态，不迁会导致这些早期订单
   上线后被当成待完成而被误点「完成订单」。pending_payment 是过渡态、生产上极少
   占用，影响面通常为 0。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'f5b9c2d7e3a1'
down_revision: Union[str, None] = 'e3f4a5b6c7d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 押金实退金额列（null = 尚未退押金）
    op.add_column(
        "orders",
        sa.Column("deposit_returned", sa.Numeric(10, 2), nullable=True),
    )

    # 2) 语义重定义配套数据迁移：早期 pending_payment → paid_pending_room（待排房）
    op.execute(
        "UPDATE orders SET order_status = 'paid_pending_room' "
        "WHERE order_status = 'pending_payment'"
    )


def downgrade() -> None:
    # 数据迁移不可逆（无法区分迁移前后的 paid_pending_room），仅回滚加列。
    op.drop_column("orders", "deposit_returned")
