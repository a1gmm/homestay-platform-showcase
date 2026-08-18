from sqlalchemy import String, Date, Numeric, ForeignKey, Integer, Index, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import date, datetime
from decimal import Decimal

from app.core.database import Base


class OrderRoom(Base):
    """订单房间明细（一张订单可挂多间房，每间房有独立的入住-退房日期和价格）。

    迁移前：orders 表自带 room_id / check_in_date / check_out_date / list_price，
    一单一房。迁移后：order_rooms 接管这些字段，orders.room_id 保留 6 个月做读
    兼容（详见 alembic/versions/<order_rooms migration>.py 注释）。
    """
    __tablename__ = "order_rooms"
    __table_args__ = (
        Index("ix_order_rooms_order", "order_id"),
        Index("ix_order_rooms_room_dates", "room_id", "check_in_date", "check_out_date"),
    )

    order_room_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    order_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False
    )
    # room_id nullable 表示"待排房"
    room_id: Mapped[str | None] = mapped_column(String(10), ForeignKey("rooms.room_id"))
    check_in_date: Mapped[date] = mapped_column(Date, nullable=False)
    check_out_date: Mapped[date] = mapped_column(Date, nullable=False)
    # issue #103 Step 3: 历史手填单 list_price 多为 NULL（未知挂牌价）。
    # 任何基于 list_price 的取数/分析必须 WHERE list_price IS NOT NULL，排除历史空值。
    list_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    discount_amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), default=Decimal("0"))
    actual_price: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))
    guests_count: Mapped[int] = mapped_column(Integer, default=0)
    # 同订单内多房的展示顺序（前端 Form.List 拖拽用）
    position: Mapped[int] = mapped_column(Integer, default=0)
    # 单间退房标记（per-room checkout）：NULL=在住/未退，非空=该房已退房的时刻。
    # 多房单允许逐间退房而不连带整单：退某间只置该行 checked_out_at + 该房待保洁，
    # 订单 order_status 保持 checked_in；退到最后一间（无 checked_out_at IS NULL 的已排房行）
    # 时订单才整体转 pending_checkout。整单退房路径会把所有在住行一并打上此时刻。
    checked_out_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 单间入住标记（per-room checkin，镜像 checked_out_at）：NULL=未入住，非空=该房已入住的时刻。
    # 多房单允许逐间办理入住：入住某间只置该行 checked_in_at + 该房 occupied + 只下这间的门锁码，
    # 订单在首间入住时转 checked_in，其余未入住行保持 reserved。整单入住路径会把所有已排房行一并打上此时刻。
    checked_in_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # 每日房价快照 { "YYYY-MM-DD": "253.38" }。值用 string 存避免 JSONB float 精度损失。
    # 创建/整体替换 OrderRoom 时按 actual_price/nights 均摊（首日吸收误差），保证 sum == actual_price。
    # 通过 PATCH /orders/{oid}/rooms/{orid}/daily-price 单日修改后，actual_price 重算 = sum(daily_prices)。
    daily_prices: Mapped[dict] = mapped_column(
        JSONB, default=dict, server_default="{}", nullable=False
    )
    # 换房溯源：{ "transfer": {reason, from_room/to_room, date, sibling} }。
    # 由 services/order_transfer.apply_room_transfer 写入，前端可据此标「本单因换房拆分」。
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, default=dict, server_default="{}", nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def nights(self) -> int:
        if self.check_in_date and self.check_out_date:
            return (self.check_out_date - self.check_in_date).days
        return 0

    @property
    def ota_owner_revenue(self) -> Decimal | None:
        """每房净房费（手填才有；metadata str 存避免 JSONB float 精度损失）。"""
        raw = (self.metadata_ or {}).get("ota_owner_revenue")
        if raw is None:
            return None
        try:
            return Decimal(str(raw))
        except Exception:
            return None

    order = relationship("Order", back_populates="rooms")
    room = relationship("Room")
