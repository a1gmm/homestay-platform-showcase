"""Date-level historical cleaning evidence confirmed from an immutable workbook."""

from datetime import date, datetime

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class CleaningWorkImport(Base):
    __tablename__ = "cleaning_work_imports"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id", "request_id", name="uq_cleaning_work_import_request"
        ),
    )
    import_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        ForeignKey("monthly_close_cycles.cycle_id"), nullable=False
    )
    document_id: Mapped[str] = mapped_column(
        ForeignKey("monthly_close_documents.document_id"), nullable=False, index=True
    )
    request_id: Mapped[str] = mapped_column(String(80), nullable=False)
    preview_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CleaningWorkRecord(Base):
    __tablename__ = "cleaning_work_records"
    __table_args__ = (
        UniqueConstraint(
            "room_id",
            "service_date",
            "service_type",
            name="uq_cleaning_work_record_event",
        ),
        CheckConstraint(
            "service_type IN ('cleaning', 'instay_cleaning')",
            name="ck_cleaning_work_record_type",
        ),
        CheckConstraint(
            "quantity >= 1 AND quantity <= 5000",
            name="ck_cleaning_work_record_quantity",
        ),
    )
    quantity: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    record_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    import_id: Mapped[str] = mapped_column(
        ForeignKey("cleaning_work_imports.import_id"), nullable=False
    )
    room_id: Mapped[str] = mapped_column(ForeignKey("rooms.room_id"), nullable=False)
    service_date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    service_type: Mapped[str] = mapped_column(String(24), nullable=False)
    document_id: Mapped[str] = mapped_column(
        ForeignKey("monthly_close_documents.document_id"), nullable=False, index=True
    )
    document_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sheet: Mapped[str] = mapped_column(String(255), nullable=False)
    source_row: Mapped[int] = mapped_column(Integer, nullable=False)
    created_by: Mapped[str] = mapped_column(ForeignKey("users.user_id"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
