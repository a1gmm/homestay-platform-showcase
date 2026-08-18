from sqlalchemy import String, Numeric, Date, Text, DateTime, ForeignKey, func, Index, UniqueConstraint
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from app.core.database import Base


class PricingRecord(Base):
    """Intelligent pricing records with full audit trail."""
    __tablename__ = "pricing_records"
    __table_args__ = (
        UniqueConstraint("room_id", "effective_date", name="uq_pricing_room_date"),
        Index("ix_pricing_room_date", "room_id", "effective_date"),
    )

    pricing_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    room_id: Mapped[Optional[str]] = mapped_column(String(10), ForeignKey("rooms.room_id"))
    effective_date: Mapped[date] = mapped_column(Date, nullable=False)
    price: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    # Dynamic pricing fields
    recommended_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    base_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    competitor_avg_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(10, 2))
    algorithm_factors: Mapped[Optional[dict]] = mapped_column(JSONB)
    source: Mapped[Optional[str]] = mapped_column(String(50))  # rule_engine / competitor / manual
    source_url: Mapped[Optional[str]] = mapped_column(Text)
    notes: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[str]] = mapped_column(String(20), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
