from sqlalchemy import String, Text, Integer, BigInteger, DateTime, ForeignKey, func, Index
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from datetime import datetime

from app.core.database import Base


class AuditLog(Base):
    __tablename__ = "audit_logs"
    __table_args__ = (
        Index("ix_audit_resource", "resource_type", "resource_id"),
        Index("ix_audit_created", "created_at"),
    )

    # BIGINT on Postgres (existing column type), INTEGER on SQLite so the test DB gets
    # ROWID-aliased autoincrement. No prod schema change.
    log_id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer(), "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    operator_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    action: Mapped[str] = mapped_column(String(100), nullable=False)  # e.g. "order.create"
    resource_type: Mapped[str | None] = mapped_column(String(50))  # e.g. "order"
    resource_id: Mapped[str | None] = mapped_column(String(50))
    before_data: Mapped[dict | None] = mapped_column(JSONB)
    after_data: Mapped[dict | None] = mapped_column(JSONB)
    ip_address: Mapped[str | None] = mapped_column(String(50))
    user_agent: Mapped[str | None] = mapped_column(String(300))
    notes: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())

    operator = relationship("User", back_populates="audit_logs")
