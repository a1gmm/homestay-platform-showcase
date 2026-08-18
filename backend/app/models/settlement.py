from sqlalchemy import (
    String, Numeric, Text, Enum as PgEnum, DateTime, Date, ForeignKey, Integer,
    UniqueConstraint, func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy import event, inspect
from datetime import date, datetime
from decimal import Decimal
import enum

from app.core.database import Base


class SettlementStatus(str, enum.Enum):
    pending = "pending"
    confirmed = "confirmed"
    paid = "paid"
    disputed = "disputed"


class OwnerSettlement(Base):
    __tablename__ = "owner_settlements"

    settlement_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(20), ForeignKey("owners.owner_id"), nullable=False)
    billing_month: Mapped[str] = mapped_column(String(7), nullable=False)  # YYYY-MM
    total_net_revenue: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    owner_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    deducted_expenses: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    actual_owner_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    status: Mapped[SettlementStatus] = mapped_column(
        PgEnum(SettlementStatus, name="settlement_status"), default=SettlementStatus.pending
    )
    payment_date: Mapped[date | None] = mapped_column(Date)
    doc_url: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    owner = relationship("Owner", back_populates="settlements")
    items = relationship(
        "OwnerSettlementItem",
        back_populates="settlement",
        cascade="all, delete-orphan",
    )


class OwnerSettlementItem(Base):
    """单套房在某月结算的明细快照"""
    __tablename__ = "owner_settlement_items"
    __table_args__ = (
        UniqueConstraint(
            "item_id", "settlement_id", name="uq_owner_settlement_items_item_batch"
        ),
    )

    item_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    settlement_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("owner_settlements.settlement_id", ondelete="CASCADE"), nullable=False
    )
    # 业主级支出行（整层/某业主电费等，room_id 为空、只挂 owner）此字段为 NULL，
    # 前端按 label 展示；普通逐房行仍必有 room_id。
    room_id: Mapped[str | None] = mapped_column(String(10), ForeignKey("rooms.room_id"), nullable=True)
    # 业主级支出行的展示标签（如「电费·整层」）。逐房行为空，前端回落到房名。
    label: Mapped[str | None] = mapped_column(String(50), nullable=True)
    # 多房订单按 order_room 粒度拆账后，每条 settlement_item 挂到具体的 order_room；
    # 老数据（迁移前生成的结算单）此字段为 NULL，前端展示时按 room_id 兜底。
    order_room_id: Mapped[str | None] = mapped_column(
        String(24), ForeignKey("order_rooms.order_room_id"), nullable=True
    )
    order_count: Mapped[int] = mapped_column(Integer, default=0)
    revenue: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    commission: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    net_revenue: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    # 已由渠道直接结给业主的收入仍展示在 net_revenue，但不再进入公司打款。
    # 单独快照，避免把它伪装成 owner_expenses。
    externally_settled_income: Mapped[Decimal] = mapped_column(
        Numeric(10, 2), default=Decimal("0"), server_default="0", nullable=False
    )
    # Append-only accounting line created for a sponsorship delta after this
    # settlement snapshot was generated.  NULL marks an original snapshot row.
    sponsorship_adjustment_id: Mapped[str | None] = mapped_column(
        String(40),
        ForeignKey("company_sponsorship_adjustments.adjustment_id"),
        unique=True,
        nullable=True,
    )
    owner_expenses: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    share_ratio_snapshot: Mapped[Decimal] = mapped_column(Numeric(4, 3), default=Decimal("0.600"))
    owner_net_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    # v2-issue#4: 每笔费用按 RoomCostShareRule 加权后的明细。结构：
    # [{"expense_id": "EXP-...", "category": "cleaning", "booking_type": "normal",
    #   "amount": "100.00", "share_percent": "0.5000", "owner_amount": "50.00"}]
    # 业主端展示费用拆解用（v2-issue#5）。老数据无值时为空数组。
    cost_share_breakdown: Mapped[list] = mapped_column(
        "cost_share_breakdown", JSONB, default=list, server_default="[]", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    settlement = relationship("OwnerSettlement", back_populates="items")


def _reject_sponsorship_correction_line_mutation(
    _mapper, _connection, target: OwnerSettlementItem
) -> None:
    state = inspect(target)
    history = state.attrs.sponsorship_adjustment_id.history
    persisted_correction = target.sponsorship_adjustment_id is not None
    was_correction = any(value is not None for value in history.deleted)
    if persisted_correction or was_correction:
        raise ValueError("sponsorship settlement correction lines are append-only")


def _reject_sponsorship_correction_line_delete(
    _mapper, _connection, target: OwnerSettlementItem
) -> None:
    if target.sponsorship_adjustment_id is not None:
        raise ValueError("sponsorship settlement correction lines are append-only")


event.listen(
    OwnerSettlementItem,
    "before_update",
    _reject_sponsorship_correction_line_mutation,
)
event.listen(
    OwnerSettlementItem,
    "before_delete",
    _reject_sponsorship_correction_line_delete,
)
