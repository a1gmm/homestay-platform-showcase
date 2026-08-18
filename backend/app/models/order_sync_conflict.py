"""Persistent, deduplicated upstream values blocked by manual field ownership."""
from datetime import datetime
import enum

from sqlalchemy import DateTime, Enum as PgEnum, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class OrderSyncConflictStatus(str, enum.Enum):
    open = "open"
    ignored = "ignored"
    resolved = "resolved"


class OrderSyncConflict(Base):
    __tablename__ = "order_sync_conflicts"
    __table_args__ = (
        UniqueConstraint(
            "source_order_id", "field", "upstream_version",
            name="uq_order_sync_conflicts_source_field_version",
        ),
        Index("ix_order_sync_conflicts_source_status", "source_order_id", "status"),
        Index("ix_order_sync_conflicts_status_last_seen", "status", "last_seen_at"),
    )

    conflict_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source_order_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("orders.order_id"), nullable=False
    )
    field: Mapped[str] = mapped_column(String(50), nullable=False)
    # Values are canonical JSON-safe snapshots (scalars or objects), never ORM values.
    local_value: Mapped[object] = mapped_column(JSONB, nullable=False)
    upstream_value: Mapped[object] = mapped_column(JSONB, nullable=False)
    upstream_version: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[OrderSyncConflictStatus] = mapped_column(
        PgEnum(OrderSyncConflictStatus, name="order_sync_conflict_status"),
        nullable=False,
        default=OrderSyncConflictStatus.open,
        server_default="open",
    )
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ignored_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    ignored_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ignored_audit_log_id: Mapped[int | None] = mapped_column(ForeignKey("audit_logs.log_id"))
    resolved_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_audit_log_id: Mapped[int | None] = mapped_column(ForeignKey("audit_logs.log_id"))
