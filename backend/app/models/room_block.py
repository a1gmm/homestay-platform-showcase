from sqlalchemy import String, Date, Text, DateTime, ForeignKey, func, Index
from sqlalchemy.orm import Mapped, mapped_column
from datetime import date, datetime
from typing import Optional
import enum

from app.core.database import Base


class BlockType(str, enum.Enum):
    owner_use = "owner_use"
    maintenance = "maintenance"
    reserved = "reserved"
    other = "other"


class RoomBlock(Base):
    __tablename__ = "room_blocks"
    __table_args__ = (
        Index("ix_room_blocks_room_dates", "room_id", "start_date", "end_date"),
    )

    block_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    room_id: Mapped[str] = mapped_column(String(10), ForeignKey("rooms.room_id"), nullable=False)
    block_type: Mapped[BlockType] = mapped_column(String(20), nullable=False)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason: Mapped[Optional[str]] = mapped_column(Text)
    created_by: Mapped[Optional[str]] = mapped_column(String(20), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
