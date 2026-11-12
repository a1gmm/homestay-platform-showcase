"""Administrator support records, isolated from financial evidence and AI input."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, Text, UniqueConstraint, CheckConstraint, Index, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class MonthlyCloseFeedback(Base):
    __tablename__ = "monthly_close_feedback"
    __table_args__ = (
        UniqueConstraint("user_id", "request_id", name="uq_mc_feedback_request"),
        CheckConstraint("status IN ('received','processing','resolved')", name="ck_mc_feedback_status"),
        Index("ix_mc_feedback_month_created", "billing_month", "created_at"),
    )

    feedback_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[str] = mapped_column(String(20), ForeignKey("users.user_id"), nullable=False)
    request_id: Mapped[str] = mapped_column(String(36), nullable=False)
    payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    billing_month: Mapped[str] = mapped_column(String(7), nullable=False)
    step: Mapped[str] = mapped_column(String(40), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    expected: Mapped[str] = mapped_column(Text, nullable=False, default="")
    execution_clicked: Mapped[str] = mapped_column(String(10), nullable=False)
    screenshot_mime: Mapped[str | None] = mapped_column(String(30))
    screenshot: Mapped[bytes | None] = mapped_column(LargeBinary, deferred=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="received", server_default="received")
    reply: Mapped[str] = mapped_column(Text, nullable=False, default="", server_default="")
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())
