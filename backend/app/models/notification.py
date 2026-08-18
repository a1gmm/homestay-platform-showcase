from sqlalchemy import String, Text, Boolean, DateTime, ForeignKey, func
from sqlalchemy.orm import Mapped, mapped_column
from datetime import datetime
import enum

from app.core.database import Base


class NotificationType(str, enum.Enum):
    checkin_guide = "checkin_guide"
    system = "system"
    task = "task"
    order = "order"
    settlement = "settlement"
    alert = "alert"


class NotificationChannel(str, enum.Enum):
    feishu = "feishu"
    sms = "sms"
    wechat = "wechat"
    in_app = "in_app"


class Notification(Base):
    __tablename__ = "notifications"

    notification_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    user_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    type: Mapped[NotificationType] = mapped_column(
        String(30), default=NotificationType.system
    )
    is_read: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class NotificationLog(Base):
    __tablename__ = "notification_logs"

    log_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    order_id: Mapped[str | None] = mapped_column(String(20))
    template_name: Mapped[str | None] = mapped_column(String(50))
    channel: Mapped[str] = mapped_column(String(20), default="feishu")
    recipient: Mapped[str | None] = mapped_column(String(100))
    content: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="sent")
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
