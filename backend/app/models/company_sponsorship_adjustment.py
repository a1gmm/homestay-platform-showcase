"""Append-only corrections to a company sponsorship root fact."""

from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    event,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base


class CompanySponsorshipAdjustment(Base):
    __tablename__ = "company_sponsorship_adjustments"
    __table_args__ = (
        UniqueConstraint(
            "sponsorship_id",
            "operation_key",
            name="uq_sponsorship_adjustments_root_operation",
        ),
        CheckConstraint("delta <> 0", name="ck_sponsorship_adjustment_nonzero_delta"),
        CheckConstraint(
            "length(trim(reason)) > 0",
            name="ck_sponsorship_adjustment_nonblank_reason",
        ),
        CheckConstraint(
            "length(trim(operation_key)) > 0",
            name="ck_sponsorship_adjustment_nonblank_operation_key",
        ),
        CheckConstraint(
            "actor_id IS NULL OR length(trim(actor_id)) > 0",
            name="ck_sponsorship_adjustment_nonblank_actor",
        ),
        CheckConstraint(
            "system_principal IS NULL OR length(trim(system_principal)) > 0",
            name="ck_sponsorship_adjustment_nonblank_system_principal",
        ),
        CheckConstraint(
            "CAST(delta AS TEXT) NOT IN ('NaN', 'Infinity', '-Infinity', 'Inf', '-Inf')",
            name="ck_sponsorship_adjustment_finite_delta",
        ),
        CheckConstraint(
            "(actor_id IS NOT NULL AND system_principal IS NULL) OR "
            "(actor_id IS NULL AND system_principal IS NOT NULL)",
            name="ck_sponsorship_adjustment_one_principal",
        ),
        Index("ix_sponsorship_adjustments_root_created", "sponsorship_id", "created_at"),
    )

    adjustment_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    sponsorship_id: Mapped[str] = mapped_column(
        String(40), ForeignKey("company_sponsored_stays.sponsored_stay_id"), nullable=False
    )
    delta: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    operation_key: Mapped[str] = mapped_column(String(255), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    system_principal: Mapped[str | None] = mapped_column(String(80))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    sponsorship = relationship("CompanySponsoredStay", back_populates="adjustments")

    @property
    def correction_of_id(self) -> str:
        """Domain/read-model name for the persisted sponsorship_id foreign key."""
        return self.sponsorship_id


def _reject_adjustment_mutation(*_args, **_kwargs) -> None:
    raise ValueError("company sponsorship adjustments are append-only")


event.listen(CompanySponsorshipAdjustment, "before_update", _reject_adjustment_mutation)
event.listen(CompanySponsorshipAdjustment, "before_delete", _reject_adjustment_mutation)
