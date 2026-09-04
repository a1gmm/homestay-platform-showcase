"""Persistent state for the administrator-only nine-step monthly close."""

from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


MONTHLY_CLOSE_STEP_KEYS = (
    "source_collection",
    "order_integrity",
    "service_fees",
    "utilities",
    "ota_statements",
    "exception_clearance",
    "preflight",
    "settlement_review",
    "owner_confirmation",
)

MONTHLY_CLOSE_SOURCE_TYPES = (
    "cleaning_statement",
    "linen_statement",
    "utility_receipt",
    "utility_expense",
    "ota_statement",
    "operating_expenses",
)

MONTHLY_CLOSE_INBOX_STATUSES = (
    "received",
    "classified",
    "needs_review",
    "confirmed",
    "failed",
    "dismissed",
)

MONTHLY_CLOSE_INBOX_ORIGINS = ("admin", "external")


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


class MonthlyCloseCycle(Base):
    __tablename__ = "monthly_close_cycles"
    __table_args__ = (
        UniqueConstraint("billing_month", name="uq_monthly_close_cycles_month"),
        CheckConstraint(
            "status IN ('open', 'completed', 'reopened')",
            name="ck_monthly_close_cycles_status",
        ),
    )

    cycle_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    billing_month: Mapped[str] = mapped_column(String(7), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="open", server_default="open"
    )
    final_snapshot: Mapped[dict | None] = mapped_column(JSONB)
    completed_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopened_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reopen_reason: Mapped[str | None] = mapped_column(Text)
    created_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    monitor_state_hash: Mapped[str | None] = mapped_column(String(64))
    monitor_state_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    monitor_effective_status: Mapped[str | None] = mapped_column(String(20))
    monitor_current_step: Mapped[str | None] = mapped_column(String(40))
    monitor_progress: Mapped[int | None] = mapped_column(Integer)
    monitor_blocking_count: Mapped[int | None] = mapped_column(Integer)


class MonthlyCloseStepConfirmation(Base):
    __tablename__ = "monthly_close_step_confirmations"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id", "step_key", name="uq_monthly_close_step_cycle_key"
        ),
        CheckConstraint(
            f"step_key IN ({_sql_values(MONTHLY_CLOSE_STEP_KEYS)})",
            name="ck_monthly_close_step_key",
        ),
        Index("ix_monthly_close_step_cycle", "cycle_id"),
    )

    confirmation_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
    )
    step_key: Mapped[str] = mapped_column(String(40), nullable=False)
    evidence_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    evidence: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    history: Mapped[list] = mapped_column(
        JSONB, nullable=False, default=list, server_default="[]"
    )
    note: Mapped[str | None] = mapped_column(Text)
    confirmed_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MonthlyCloseSourceRequirement(Base):
    __tablename__ = "monthly_close_source_requirements"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id", "source_type", name="uq_monthly_close_source_cycle_type"
        ),
        CheckConstraint(
            f"source_type IN ({_sql_values(MONTHLY_CLOSE_SOURCE_TYPES)})",
            name="ck_monthly_close_source_type",
        ),
        CheckConstraint(
            "state IN ('pending', 'uploaded', 'not_applicable')",
            name="ck_monthly_close_source_state",
        ),
    )

    requirement_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    state: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending", server_default="pending"
    )
    not_applicable_reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MonthlyCloseDocument(Base):
    __tablename__ = "monthly_close_documents"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id", "sha256", name="uq_monthly_close_document_cycle_sha"
        ),
        CheckConstraint(
            f"source_type IN ({_sql_values(MONTHLY_CLOSE_SOURCE_TYPES)})",
            name="ck_monthly_close_document_source_type",
        ),
        CheckConstraint(
            "processing_status IN ('stored', 'processed', 'rejected')",
            name="ck_monthly_close_document_status",
        ),
        Index(
            "ix_monthly_close_document_cycle_source",
            "cycle_id",
            "source_type",
            "is_active",
        ),
    )

    document_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
    )
    source_type: Mapped[str] = mapped_column(String(40), nullable=False)
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    # Original workbooks are only needed for download or a deliberate engine
    # action. Monthly-close evidence is polled frequently, so keeping the blob
    # deferred prevents every metadata read from pulling up to 10MB per file.
    content: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, deferred=True
    )
    processing_status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="stored", server_default="stored"
    )
    processing_error: Mapped[str | None] = mapped_column(Text)
    engine_type: Mapped[str | None] = mapped_column(String(40))
    engine_id: Mapped[str | None] = mapped_column(String(40))
    metadata_: Mapped[dict] = mapped_column(
        "metadata", JSONB, nullable=False, default=dict, server_default="{}"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    invalidated_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    uploaded_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    uploaded_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class MonthlyCloseInboxItem(Base):
    """Durable pre-archive receipt for admin and external workbook intake."""

    __tablename__ = "monthly_close_inbox_items"
    __table_args__ = (
        UniqueConstraint(
            "cycle_id", "sha256", name="uq_monthly_close_inbox_cycle_sha"
        ),
        CheckConstraint(
            f"status IN ({_sql_values(MONTHLY_CLOSE_INBOX_STATUSES)})",
            name="ck_monthly_close_inbox_status",
        ),
        CheckConstraint(
            f"origin IN ({_sql_values(MONTHLY_CLOSE_INBOX_ORIGINS)})",
            name="ck_monthly_close_inbox_origin",
        ),
        CheckConstraint(
            "source_type IS NULL OR source_type IN "
            f"({_sql_values(MONTHLY_CLOSE_SOURCE_TYPES)})",
            name="ck_monthly_close_inbox_source_type",
        ),
        Index("ix_monthly_close_inbox_cycle_status", "cycle_id", "status"),
    )

    item_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(120), nullable=False)
    byte_size: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[bytes] = mapped_column(
        LargeBinary, nullable=False, deferred=True
    )
    source_type: Mapped[str | None] = mapped_column(String(40))
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4))
    suggested_by: Mapped[str | None] = mapped_column(String(24))
    classification_reason: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="received", server_default="received"
    )
    last_error: Mapped[str | None] = mapped_column(Text)
    origin: Mapped[str] = mapped_column(
        String(20), nullable=False, default="admin", server_default="admin"
    )
    submitted_label: Mapped[str | None] = mapped_column(String(120))
    created_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    updated_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    document_id: Mapped[str | None] = mapped_column(
        String(24),
        ForeignKey("monthly_close_documents.document_id", ondelete="SET NULL"),
        unique=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )


class MonthlyCloseIntakeLink(Base):
    """Hash-only bearer credential that grants upload-only access to one cycle."""

    __tablename__ = "monthly_close_intake_links"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_monthly_close_intake_token_hash"),
        CheckConstraint(
            "source_type IS NULL OR source_type IN "
            f"({_sql_values(MONTHLY_CLOSE_SOURCE_TYPES)})",
            name="ck_monthly_close_intake_source_type",
        ),
        Index("ix_monthly_close_intake_cycle", "cycle_id", "revoked_at"),
    )

    link_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("monthly_close_cycles.cycle_id", ondelete="CASCADE"),
        nullable=False,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_type: Mapped[str | None] = mapped_column(String(40))
    label: Mapped[str] = mapped_column(String(120), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("users.user_id")
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_uploaded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MonthlyCloseServiceLine(Base):
    __tablename__ = "monthly_close_service_lines"
    __table_args__ = (
        UniqueConstraint(
            "document_id",
            "source_sheet",
            "source_row_number",
            name="uq_monthly_close_service_line_source_row",
        ),
        CheckConstraint(
            "match_status IN ('pending', 'matched', 'amount_mismatch', "
            "'vendor_only', 'system_only', 'ambiguous', 'duplicate')",
            name="ck_monthly_close_service_line_match_status",
        ),
        Index(
            "ix_monthly_close_service_line_document_status",
            "document_id",
            "match_status",
        ),
    )

    line_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    document_id: Mapped[str] = mapped_column(
        String(24),
        ForeignKey("monthly_close_documents.document_id", ondelete="CASCADE"),
        nullable=False,
    )
    service_date: Mapped[date | None] = mapped_column(Date)
    room_ref: Mapped[str | None] = mapped_column(String(100))
    order_ref: Mapped[str | None] = mapped_column(String(100))
    service_type: Mapped[str] = mapped_column(String(40), nullable=False)
    quantity: Mapped[Decimal | None] = mapped_column(Numeric(12, 3))
    unit_price: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    source_sheet: Mapped[str] = mapped_column(String(255), nullable=False)
    source_row_number: Mapped[int] = mapped_column(Integer, nullable=False)
    raw_values: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    business_key: Mapped[str | None] = mapped_column(String(255))
    match_status: Mapped[str] = mapped_column(
        String(24), nullable=False, default="pending", server_default="pending"
    )
    issue_code: Mapped[str | None] = mapped_column(String(60))
    matched_expense_id: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("expenses.expense_id")
    )
    match_detail: Mapped[dict] = mapped_column(
        JSONB, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
