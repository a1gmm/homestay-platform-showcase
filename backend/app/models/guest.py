from sqlalchemy import String, Integer, Numeric, Text, DateTime, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime
from decimal import Decimal
from typing import Optional

from app.core.database import Base


class Guest(Base):
    __tablename__ = "guests"
    __table_args__ = (
        Index("ix_guests_phone", "phone", unique=True),
        Index("ix_guests_name", "name"),
    )

    guest_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    name: Mapped[str] = mapped_column(String(50), nullable=False)
    phone: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    id_number: Mapped[Optional[str]] = mapped_column(String(30))
    wechat: Mapped[Optional[str]] = mapped_column(String(50))
    notes: Mapped[Optional[str]] = mapped_column(Text)
    # Aggregated stats (updated on order create/complete)
    visit_count: Mapped[int] = mapped_column(Integer, default=0)
    total_spent: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=Decimal("0"))
    total_nights: Mapped[int] = mapped_column(Integer, default=0)
    last_check_in: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True))
    preferred_room: Mapped[Optional[str]] = mapped_column(String(10))
    tags: Mapped[Optional[str]] = mapped_column(String(200))  # comma-separated: "VIP,回头客,长租"
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
