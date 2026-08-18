"""Durable idempotency claims for order-domain mutations."""
from datetime import datetime
import enum

from sqlalchemy import DateTime, Enum as PgEnum, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OrderOperationStatus(str, enum.Enum):
    in_progress = "in_progress"
    succeeded = "succeeded"
    failed = "failed"


class OrderOperation(Base):
    __tablename__ = "order_operations"
    __table_args__ = (
        UniqueConstraint(
            "property_scope", "operation", "idempotency_key",
            name="uq_order_operations_scope_operation_key",
        ),
        Index("ix_order_operations_status", "status"),
    )

    operation_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    property_scope: Mapped[str] = mapped_column(String(64), nullable=False)
    operation: Mapped[str] = mapped_column(String(50), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[OrderOperationStatus] = mapped_column(
        PgEnum(OrderOperationStatus, name="order_operation_status"),
        nullable=False,
        default=OrderOperationStatus.in_progress,
        server_default="in_progress",
    )
    result_order_ids: Mapped[list[str]] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    result_stay_group_id: Mapped[str | None] = mapped_column(String(32))
    error_code: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
