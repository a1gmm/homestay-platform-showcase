"""专用水电费对账持久化模型。"""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class UtilityReconUpload(Base):
    __tablename__ = "utility_recon_uploads"
    __table_args__ = (Index("ix_utility_recon_uploads_created_at", "created_at"),)

    upload_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    receipt_filename: Mapped[str] = mapped_column(String(255))
    expense_filename: Mapped[str] = mapped_column(String(255))
    file_fingerprints: Mapped[dict] = mapped_column(JSONB, default=dict)
    role_mapping: Mapped[dict] = mapped_column(JSONB, default=dict)
    receipt_months: Mapped[list] = mapped_column(JSONB, default=list)
    expense_months: Mapped[list] = mapped_column(JSONB, default=list)
    common_months: Mapped[list] = mapped_column(JSONB, default=list)
    preflight_stats: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="processing")
    created_by: Mapped[str] = mapped_column(String(20), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UtilityReconBatch(Base):
    __tablename__ = "utility_recon_batches"
    __table_args__ = (
        UniqueConstraint("upload_id", "month", name="uq_utility_recon_batch_upload_month"),
        Index("ix_utility_recon_batches_month_status", "month", "status"),
    )

    batch_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    upload_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("utility_recon_uploads.upload_id", ondelete="CASCADE")
    )
    month: Mapped[str] = mapped_column(String(7))
    status: Mapped[str] = mapped_column(String(20), default="open")
    raw_receipt_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    raw_expense_total: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    raw_difference: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    corrected_difference: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0"))
    raw_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    corrected_summary: Mapped[dict] = mapped_column(JSONB, default=dict)
    anomaly_counts: Mapped[dict] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    closed_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UtilityReconRow(Base):
    __tablename__ = "utility_recon_rows"
    __table_args__ = (
        Index("ix_utility_recon_rows_batch_side_date", "batch_id", "side", "business_date"),
    )

    row_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("utility_recon_batches.batch_id", ondelete="CASCADE")
    )
    side: Mapped[str] = mapped_column(String(10))
    business_date: Mapped[date | None] = mapped_column(Date)
    month: Mapped[str] = mapped_column(String(7))
    floor: Mapped[str | None] = mapped_column(String(30))
    room: Mapped[str | None] = mapped_column(String(50))
    category: Mapped[str | None] = mapped_column(String(20))
    amount: Mapped[Decimal | None] = mapped_column(Numeric(14, 2))
    source_filename: Mapped[str] = mapped_column(String(255))
    source_sheet: Mapped[str] = mapped_column(String(255))
    source_row_number: Mapped[int] = mapped_column(Integer)
    raw_values: Mapped[dict] = mapped_column(JSONB, default=dict)
    customer_name: Mapped[str | None] = mapped_column(String(100))
    normalization_warnings: Mapped[list] = mapped_column(JSONB, default=list)
    disposition: Mapped[str] = mapped_column(String(20), default="valid")
    exclusion_reason: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class UtilityReconSuggestion(Base):
    __tablename__ = "utility_recon_suggestions"
    __table_args__ = (
        Index("ix_utility_recon_suggestions_batch_status", "batch_id", "status"),
    )

    suggestion_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    batch_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("utility_recon_batches.batch_id", ondelete="CASCADE")
    )
    kind: Mapped[str] = mapped_column(String(30))
    related_row_ids: Mapped[list] = mapped_column(JSONB, default=list)
    patch: Mapped[dict] = mapped_column(JSONB, default=dict)
    evidence: Mapped[dict] = mapped_column(JSONB, default=dict)
    confidence: Mapped[str] = mapped_column(String(10))
    impact: Mapped[dict] = mapped_column(JSONB, default=dict)
    status: Mapped[str] = mapped_column(String(20), default="pending")
    decided_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
