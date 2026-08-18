"""Explicit aggregate for manually managed stay groups only."""
from datetime import datetime
import enum

from sqlalchemy import CheckConstraint, DateTime, Enum as PgEnum, ForeignKey, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class ManagedStayGroupKind(str, enum.Enum):
    managed_split = "managed_split"


class ManagedStayGroup(Base):
    __tablename__ = "managed_stay_groups"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_managed_stay_group_positive_version"),
    )

    # This reuses Order.stay_group_id as the aggregate and member association key.
    stay_group_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    source_order_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("orders.order_id"), nullable=False, unique=True
    )
    kind: Mapped[ManagedStayGroupKind] = mapped_column(
        PgEnum(ManagedStayGroupKind, name="managed_stay_group_kind"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
