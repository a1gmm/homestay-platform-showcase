"""
v2-issue#2 — 房间业主费用支出占比规则。

存储「房间 × 订单类型 × 费用类目」三维配置：业主承担多少百分比的某项支出。
对应王总参考 PMS 系统截图 4 的「业主费用支出占比」区。

数据规模：33 房 × 3 订单类型 × 14 费用类目 = 1386 行（v2-issue#6 seed）。

settlement 月度任务（v2-issue#4 改造）按 (room_id, expense.category, order.booking_type)
JOIN 此表算业主承担金额；规则不存在时 fallback 到 share_percent=1.0（业主全担保守值）。
"""
from __future__ import annotations

from decimal import Decimal
from datetime import datetime

from sqlalchemy import (
    String, Numeric, Enum as PgEnum, DateTime, ForeignKey, func, Index, UniqueConstraint
)
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base
from app.models.expense import ExpenseCategory
from app.models.order import BookingType


class RoomCostShareRule(Base):
    __tablename__ = "room_cost_share_rules"
    __table_args__ = (
        UniqueConstraint(
            "room_id", "booking_type", "expense_category",
            name="uq_room_cost_share_room_booking_category",
        ),
        Index("ix_room_cost_share_room_booking", "room_id", "booking_type"),
    )

    rule_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    room_id: Mapped[str] = mapped_column(
        String(10), ForeignKey("rooms.room_id", ondelete="CASCADE"), nullable=False
    )
    # 跟订单类型对齐：普通/试住/自住
    booking_type: Mapped[BookingType] = mapped_column(
        PgEnum(BookingType, name="booking_type"), nullable=False
    )
    expense_category: Mapped[ExpenseCategory] = mapped_column(
        PgEnum(ExpenseCategory, name="expense_category"), nullable=False
    )
    # 业主承担占比，0~1，默认 1.0 = 业主全担（与王总参考截图 100% 默认值一致）
    share_percent: Mapped[Decimal] = mapped_column(
        Numeric(5, 4), nullable=False, default=Decimal("1.0000"), server_default="1.0000"
    )
    # 固定单价（可选，与百分比互斥；存在时按 unit_price × 数量算，不再用 share_percent）
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )
