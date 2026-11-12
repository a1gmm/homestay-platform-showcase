"""Immutable correction evidence for a confirmed, unpaid owner bill."""
from datetime import datetime
from decimal import Decimal
from sqlalchemy import String, Integer, Numeric, ForeignKey, DateTime, Text, UniqueConstraint, CheckConstraint, event, func
from sqlalchemy.orm import Mapped, mapped_column
from app.core.database import Base


class SettlementFeeAdjustment(Base):
    __tablename__ = 'settlement_fee_adjustments'
    __table_args__ = (
        UniqueConstraint('original_expense_id', name='uq_settlement_fee_original'),
        UniqueConstraint('replacement_expense_id', name='uq_settlement_fee_replacement'),
        CheckConstraint('before_amount = 30 AND after_amount = 65', name='ck_self_stay_fee_correction'),
    )
    adjustment_id: Mapped[str] = mapped_column(String(24), primary_key=True)
    settlement_id: Mapped[str] = mapped_column(String(20), ForeignKey('owner_settlements.settlement_id'), nullable=False)
    original_item_id: Mapped[str] = mapped_column(String(20), ForeignKey('owner_settlement_items.item_id'), nullable=False)
    entry_index: Mapped[int] = mapped_column(Integer, nullable=False)
    original_expense_id: Mapped[str] = mapped_column(String(20), ForeignKey('expenses.expense_id'), nullable=False)
    replacement_expense_id: Mapped[str] = mapped_column(String(20), ForeignKey('expenses.expense_id'), nullable=False)
    before_amount: Mapped[Decimal] = mapped_column(Numeric(10,2), nullable=False)
    after_amount: Mapped[Decimal] = mapped_column(Numeric(10,2), nullable=False)
    proposal_id: Mapped[str] = mapped_column(String(24), ForeignKey('financial_case_proposals.proposal_id'), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(20), ForeignKey('users.user_id'), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, server_default=func.now())


def _immutable(*args, **kwargs):
    raise ValueError('settlement fee adjustments are append-only')


event.listen(SettlementFeeAdjustment, 'before_update', _immutable)
event.listen(SettlementFeeAdjustment, 'before_delete', _immutable)
