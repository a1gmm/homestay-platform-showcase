"""Immutable administrator decisions for a single workbook reconciliation event."""

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CleaningWorkResolution(Base):
    __tablename__ = "cleaning_work_resolutions"
    __table_args__ = (
        CheckConstraint(
            "confirmed_count >= 0 AND confirmed_count <= 5000",
            name="ck_cleaning_resolution_count",
        ),
        CheckConstraint(
            "decision IN ('exclude_system', 'count_once', 'accept_table')",
            name="ck_cleaning_resolution_decision",
        ),
    )
    resolution_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    import_id: Mapped[str] = mapped_column(
        ForeignKey("cleaning_work_imports.import_id"), nullable=False, unique=True
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("monthly_close_documents.document_id"), nullable=False, index=True
    )
    service_date: Mapped[date] = mapped_column(Date, nullable=False)
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.room_id"), nullable=False)
    service_type: Mapped[str] = mapped_column(String(24), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    decision: Mapped[str] = mapped_column(String(24), nullable=False)
    confirmed_count: Mapped[int] = mapped_column(Integer, nullable=False)
    reason: Mapped[str] = mapped_column(String(1000), nullable=False)
    evidence: Mapped[dict] = mapped_column(JSONB, nullable=False)
    confirmed_by: Mapped[str] = mapped_column(
        ForeignKey("users.user_id"), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
