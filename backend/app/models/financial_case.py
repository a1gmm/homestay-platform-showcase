"""Immutable source evidence and version-bound financial case proposals."""
from datetime import datetime
from sqlalchemy import String, Integer, LargeBinary, ForeignKey, UniqueConstraint, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class FinancialCaseSource(Base):
    __tablename__ = 'financial_case_sources'
    __table_args__ = (UniqueConstraint('cycle_id', 'sha256', name='uq_financial_case_source_digest'),)
    source_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(24), ForeignKey('monthly_close_cycles.cycle_id'), index=True)
    filename: Mapped[str] = mapped_column(String(255))
    sha256: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(24))
    content: Mapped[bytes] = mapped_column(LargeBinary, deferred=True)
    parsed: Mapped[dict] = mapped_column(JSONB)
    decisions: Mapped[dict] = mapped_column(JSONB, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    document_id: Mapped[str | None] = mapped_column(String(24), ForeignKey('monthly_close_documents.document_id'))
    created_by: Mapped[str] = mapped_column(String(20), ForeignKey('users.user_id'))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FinancialCaseProposal(Base):
    __tablename__ = 'financial_case_proposals'
    proposal_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    cycle_id: Mapped[str] = mapped_column(String(24), ForeignKey('monthly_close_cycles.cycle_id'), index=True)
    run_id: Mapped[str] = mapped_column(String(24), ForeignKey('monthly_close_agent_runs.run_id'), unique=True)
    created_by: Mapped[str] = mapped_column(String(20), ForeignKey('users.user_id'))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSONB)
    status: Mapped[str] = mapped_column(String(24), default='pending')
    result: Mapped[dict | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
