from sqlalchemy import String, Numeric, Text, Enum as PgEnum, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from decimal import Decimal
import enum

from app.core.database import Base


class RefundReason(str, enum.Enum):
    guest_cancel = "guest_cancel"
    host_cancel = "host_cancel"
    complaint = "complaint"
    deposit_return = "deposit_return"
    other = "other"


class Refund(Base):
    __tablename__ = "refunds"

    refund_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(20), ForeignKey("orders.order_id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    reason: Mapped[RefundReason] = mapped_column(PgEnum(RefundReason, name="refund_reason"))
    refunded_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 软删除/冲正：误录退款只能"作废"、不能物理删（保留审计追溯）。is_deleted=True 的
    # 退款从列表/累计/额度校验中剔除，口径与 Payment 软删一致（批2 item6）。
    is_deleted: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))

    order = relationship("Order", back_populates="refunds")
