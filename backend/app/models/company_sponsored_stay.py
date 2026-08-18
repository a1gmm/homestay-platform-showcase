"""Company-sponsored child segment root financial fact."""

from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum as PgEnum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    event,
    func,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.order import Channel, Order, StaySettlementKind
from app.models.order_source_price_snapshot import OrderSourcePriceSnapshot
from app.models.managed_stay_group import ManagedStayGroup, ManagedStayGroupKind
from app.models.settlement import OwnerSettlementItem


class PaymentResponsibility(StrEnum):
    company_payable = "company_payable"
    channel_settled = "channel_settled"


class CompanySponsorshipStatus(StrEnum):
    confirmed = "confirmed"
    settled = "settled"
    voided = "voided"


class CompanySponsoredStay(Base):
    __tablename__ = "company_sponsored_stays"
    __table_args__ = (
        CheckConstraint("segment_check_out_date > segment_check_in_date", name="ck_sponsored_stay_dates"),
        CheckConstraint("calculation_base >= 0", name="ck_sponsored_stay_nonnegative_base"),
        CheckConstraint(
            "settlement_ratio >= 0 AND settlement_ratio <= 1",
            name="ck_sponsored_stay_ratio_range",
        ),
        CheckConstraint("amount >= 0", name="ck_sponsored_stay_nonnegative_amount"),
        CheckConstraint("version >= 1", name="ck_sponsored_stay_positive_version"),
        CheckConstraint(
            "(settlement_item_id IS NULL AND settlement_batch_id IS NULL) OR "
            "(settlement_item_id IS NOT NULL AND settlement_batch_id IS NOT NULL)",
            name="ck_sponsored_stay_settlement_refs_pair",
        ),
        CheckConstraint(
            "status <> 'settled' OR "
            "(settlement_item_id IS NOT NULL AND settlement_batch_id IS NOT NULL)",
            name="ck_sponsored_stay_settled_refs",
        ),
        ForeignKeyConstraint(
            ["settlement_item_id", "settlement_batch_id"],
            ["owner_settlement_items.item_id", "owner_settlement_items.settlement_id"],
            name="fk_sponsored_stay_settlement_item_batch",
        ),
        Index("ix_company_sponsored_stays_source", "source_order_id"),
        Index(
            "uq_company_sponsored_stays_active_identity",
            "source_order_id",
            "segment_check_in_date",
            "segment_check_out_date",
            unique=True,
            postgresql_where=text("status <> 'voided'"),
            sqlite_where=text("status <> 'voided'"),
        ),
    )

    sponsored_stay_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source_order_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("orders.order_id"), nullable=False
    )
    segment_order_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("orders.order_id"), nullable=False, unique=True
    )
    # Frozen segment identity keeps the partial uniqueness rule enforceable without
    # reaching through an orders FK and survives later operational date edits.
    segment_check_in_date: Mapped[date] = mapped_column(Date, nullable=False)
    segment_check_out_date: Mapped[date] = mapped_column(Date, nullable=False)
    channel: Mapped[Channel] = mapped_column(PgEnum(Channel, name="channel"), nullable=False)
    calculation_base: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    settlement_ratio: Mapped[Decimal] = mapped_column(Numeric(5, 4), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=1, server_default="1"
    )
    payment_responsibility: Mapped[PaymentResponsibility] = mapped_column(
        PgEnum(PaymentResponsibility, name="sponsorship_payment_responsibility"),
        nullable=False,
        default=PaymentResponsibility.company_payable,
        server_default="company_payable",
    )
    status: Mapped[CompanySponsorshipStatus] = mapped_column(
        PgEnum(CompanySponsorshipStatus, name="company_sponsorship_status"),
        nullable=False,
        default=CompanySponsorshipStatus.confirmed,
        server_default="confirmed",
    )
    source_price_snapshot_id: Mapped[str] = mapped_column(
        String(40),
        ForeignKey("order_source_price_snapshots.source_price_snapshot_id"),
        nullable=False,
    )
    settlement_item_id: Mapped[str | None] = mapped_column(
        String(20)
    )
    settlement_batch_id: Mapped[str | None] = mapped_column(
        String(20), ForeignKey("owner_settlements.settlement_id")
    )
    created_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    updated_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    source_order = relationship("Order", foreign_keys=[source_order_id])
    segment_order = relationship(
        "Order", foreign_keys=[segment_order_id], back_populates="company_sponsorship"
    )
    source_price_snapshot = relationship("OrderSourcePriceSnapshot")
    adjustments = relationship(
        "CompanySponsorshipAdjustment",
        back_populates="sponsorship",
        order_by=(
            "CompanySponsorshipAdjustment.created_at, "
            "CompanySponsorshipAdjustment.adjustment_id"
        ),
        lazy="selectin",
    )

    @property
    def effective_amount(self) -> Decimal:
        return self.amount + sum(
            (adjustment.delta for adjustment in self.adjustments), Decimal("0.00")
        )


_ROOT_FACT_FIELDS = (
    "source_order_id",
    "segment_order_id",
    "segment_check_in_date",
    "segment_check_out_date",
    "channel",
    "calculation_base",
    "settlement_ratio",
    "amount",
    "payment_responsibility",
    "source_price_snapshot_id",
)


def _reject_root_fact_mutation(_mapper, _connection, target: CompanySponsoredStay) -> None:
    state = inspect(target)
    if any(state.attrs[field].history.has_changes() for field in _ROOT_FACT_FIELDS):
        raise ValueError("company sponsorship root financial facts are immutable; append an adjustment")


def _reject_terminal_status_transition(
    _mapper, _connection, target: CompanySponsoredStay
) -> None:
    history = inspect(target).attrs.status.history
    if not history.has_changes() or not history.deleted:
        return
    previous = history.deleted[0]
    if previous == CompanySponsorshipStatus.settled:
        raise ValueError("settled sponsorship status is immutable; append an adjustment")
    if previous == CompanySponsorshipStatus.voided:
        raise ValueError("voided sponsorship status is immutable")


def _reject_root_delete(*_args, **_kwargs) -> None:
    raise ValueError("company sponsorship roots are immutable and cannot be deleted")


def _validate_root_consistency(
    _mapper, connection, target: CompanySponsoredStay
) -> None:
    source = connection.execute(
        select(Order.channel, Order.check_in_date, Order.check_out_date).where(
            Order.order_id == target.source_order_id,
            Order.is_deleted.is_(False),
        )
    ).one_or_none()
    segment = connection.execute(
        select(
            Order.stay_settlement_kind,
            Order.check_in_date,
            Order.check_out_date,
        ).where(
            Order.order_id == target.segment_order_id,
            Order.is_deleted.is_(False),
        )
    ).one_or_none()
    snapshot = connection.execute(
        select(
            OrderSourcePriceSnapshot.source_order_id,
            OrderSourcePriceSnapshot.channel,
            OrderSourcePriceSnapshot.check_in_date,
            OrderSourcePriceSnapshot.check_out_date,
            OrderSourcePriceSnapshot.nightly_bases,
        ).where(
            OrderSourcePriceSnapshot.source_price_snapshot_id
            == target.source_price_snapshot_id
        )
    ).mappings().one_or_none()
    if source is None or segment is None or snapshot is None:
        raise ValueError("company sponsorship root references must be consistent")
    if (
        snapshot["source_order_id"] != target.source_order_id
        or source.channel != snapshot["channel"]
        or target.channel != source.channel
        or segment.stay_settlement_kind != StaySettlementKind.company_sponsored
        or target.segment_check_in_date != segment.check_in_date
        or target.segment_check_out_date != segment.check_out_date
        or target.segment_check_in_date < snapshot["check_in_date"]
        or target.segment_check_out_date > snapshot["check_out_date"]
    ):
        raise ValueError("company sponsorship root references must be consistent")

    expected_dates = (
        target.segment_check_in_date,
        target.segment_check_out_date,
    )
    try:
        nightly = {
            date.fromisoformat(raw_date): Decimal(str(amount))
            for raw_date, amount in snapshot["nightly_bases"].items()
        }
        selected_total = sum(
            (
                amount
                for stay_date, amount in nightly.items()
                if expected_dates[0] <= stay_date < expected_dates[1]
            ),
            Decimal("0.00"),
        )
    except (AttributeError, InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("company sponsorship root references must be consistent") from exc
    if selected_total != target.calculation_base:
        raise ValueError("company sponsorship root calculation must be snapshot-consistent")

    has_item = target.settlement_item_id is not None
    has_batch = target.settlement_batch_id is not None
    if has_item != has_batch:
        raise ValueError("settlement item and batch references must be provided together")
    if target.status == CompanySponsorshipStatus.settled and not has_item:
        raise ValueError("settled sponsorship requires settlement item and batch references")
    if has_item:
        item_batch = connection.execute(
            select(OwnerSettlementItem.settlement_id).where(
                OwnerSettlementItem.item_id == target.settlement_item_id
            )
        ).scalar_one_or_none()
        if item_batch != target.settlement_batch_id:
            raise ValueError("settlement item must belong to the same settlement batch")


event.listen(CompanySponsoredStay, "before_update", _reject_root_fact_mutation)
event.listen(CompanySponsoredStay, "before_update", _reject_terminal_status_transition)
event.listen(CompanySponsoredStay, "before_insert", _validate_root_consistency)
event.listen(CompanySponsoredStay, "before_update", _validate_root_consistency)
event.listen(CompanySponsoredStay, "before_delete", _reject_root_delete)


def _reject_bound_order_identity_mutation(_mapper, connection, target: Order) -> None:
    state = inspect(target)
    source_date_changed = any(
        state.attrs[field].history.has_changes()
        for field in ("check_in_date", "check_out_date")
    )
    source_critical_changed = any(
        state.attrs[field].history.has_changes() for field in ("channel", "is_deleted")
    )
    source_identity_changed = source_date_changed or source_critical_changed
    segment_identity_changed = any(
        state.attrs[field].history.has_changes()
        for field in ("stay_settlement_kind", "check_in_date", "check_out_date", "is_deleted")
    )
    source_is_bound = source_identity_changed and connection.execute(
        select(OrderSourcePriceSnapshot.source_price_snapshot_id)
        .where(OrderSourcePriceSnapshot.source_order_id == target.order_id)
        .limit(1)
    ).scalar_one_or_none()
    segment_is_bound = segment_identity_changed and connection.execute(
        select(CompanySponsoredStay.sponsored_stay_id)
        .where(CompanySponsoredStay.segment_order_id == target.order_id)
        .limit(1)
    ).scalar_one_or_none()
    membership_history = state.attrs["stay_group_id"].history
    initially_attached = (
        source_date_changed
        and membership_history.has_changes()
        and target.stay_group_id is not None
        and not any(value is not None for value in membership_history.deleted)
    )
    initial_split = initially_attached and connection.execute(
        select(ManagedStayGroup.stay_group_id)
        .where(
            ManagedStayGroup.source_order_id == target.order_id,
            ManagedStayGroup.stay_group_id == target.stay_group_id,
            ManagedStayGroup.kind == ManagedStayGroupKind.managed_split,
            ManagedStayGroup.version == 1,
        )
        .limit(1)
    ).scalar_one_or_none()
    if source_is_bound and (source_critical_changed or not initial_split):
        raise ValueError("order sponsorship identity is bound and cannot change")
    if segment_is_bound:
        raise ValueError("order sponsorship identity is bound and cannot change")


def _reject_bound_settlement_item_reparent(
    _mapper, connection, target: OwnerSettlementItem
) -> None:
    if not inspect(target).attrs.settlement_id.history.has_changes():
        return
    is_bound = connection.execute(
        select(CompanySponsoredStay.sponsored_stay_id)
        .where(CompanySponsoredStay.settlement_item_id == target.item_id)
        .limit(1)
    ).scalar_one_or_none()
    if is_bound:
        raise ValueError("settlement item is bound to a sponsorship root")


event.listen(Order, "before_update", _reject_bound_order_identity_mutation)
event.listen(OwnerSettlementItem, "before_update", _reject_bound_settlement_item_reparent)
