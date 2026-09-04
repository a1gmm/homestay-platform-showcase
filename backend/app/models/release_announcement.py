"""Durable per-account acknowledgements for code-owned release announcements."""
from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ReleaseAnnouncementReceipt(Base):
    __tablename__ = "release_announcement_receipts"

    user_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("users.user_id", ondelete="CASCADE"), primary_key=True
    )
    announcement_id: Mapped[str] = mapped_column(String(100), primary_key=True)
    acknowledged_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
