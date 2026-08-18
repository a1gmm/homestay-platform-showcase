from sqlalchemy import String, Integer, Boolean, Text, DateTime, ForeignKey, func, Index
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime

from app.core.database import Base


class RoomImage(Base):
    __tablename__ = "room_images"
    __table_args__ = (
        Index("ix_room_images_room_sort", "room_id", "sort_order"),
    )

    image_id: Mapped[str] = mapped_column(String(20), primary_key=True)
    room_id: Mapped[str] = mapped_column(
        String(10),
        ForeignKey("rooms.room_id", ondelete="CASCADE"),
        nullable=False,
    )
    url: Mapped[str] = mapped_column(Text, nullable=False)
    object_key: Mapped[str] = mapped_column(Text, nullable=False)
    sort_order: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    is_cover: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    content_type: Mapped[str | None] = mapped_column(String(50))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    uploaded_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )

    room = relationship("Room", back_populates="images")
