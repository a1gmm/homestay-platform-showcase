from sqlalchemy import String, Numeric, Text, Enum as PgEnum, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from decimal import Decimal
import enum

from app.core.database import Base


class PaymentMethod(str, enum.Enum):
    wechat = "wechat"
    alipay = "alipay"
    cash = "cash"
    bank_transfer = "bank_transfer"
    platform = "platform"
    other = "other"


class Payment(Base):
    __tablename__ = "payments"

    payment_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(20), ForeignKey("orders.order_id"), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    method: Mapped[PaymentMethod] = mapped_column(PgEnum(PaymentMethod, name="payment_method"))
    paid_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    is_deposit: Mapped[bool] = mapped_column(default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # 软删除：王总反馈"收错款只能新增、不能撤销"。is_deleted=True 的记录从列表/聚合中过滤，
    # 但保留行用于审计追溯，避免硬删后无法回溯历史。
    is_deleted: Mapped[bool] = mapped_column(default=False, nullable=False, server_default="false")
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))

    order = relationship("Order", back_populates="payments")
