"""Immutable, versioned upstream price facts used by sponsored stay splits."""

from datetime import date, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum

from sqlalchemy import (
    CheckConstraint,
    Date,
    DateTime,
    Enum as PgEnum,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    event,
    func,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base
from app.models.audit_log import AuditLog
from app.models.order import Channel, Order
from app.models.user import User, UserRole


class SourcePriceSnapshotOrigin(StrEnum):
    bypms_import = "bypms_import"
    bypms_adoption = "bypms_adoption"
    administrator_fallback = "administrator_fallback"
    administrator_override = "administrator_override"


class OrderSourcePriceSnapshot(Base):
    __tablename__ = "order_source_price_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "source_order_id", "version", name="uq_source_price_snapshots_order_version"
        ),
        UniqueConstraint(
            "source_order_id",
            "upstream_payload_hash",
            "origin",
            name="uq_source_price_snapshots_order_payload_origin",
        ),
        CheckConstraint("version > 0", name="ck_source_price_snapshot_positive_version"),
        CheckConstraint("check_out_date > check_in_date", name="ck_source_price_snapshot_dates"),
        CheckConstraint("total >= 0", name="ck_source_price_snapshot_nonnegative_total"),
        CheckConstraint(
            "origin NOT IN ('administrator_fallback', 'administrator_override') "
            "OR (created_by IS NOT NULL AND audit_log_id IS NOT NULL)",
            name="ck_source_price_snapshot_audited_fallback",
        ),
        Index("ix_source_price_snapshots_source", "source_order_id"),
    )

    source_price_snapshot_id: Mapped[str] = mapped_column(String(40), primary_key=True)
    source_order_id: Mapped[str] = mapped_column(
        String(20), ForeignKey("orders.order_id"), nullable=False
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    upstream_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    channel: Mapped[Channel] = mapped_column(PgEnum(Channel, name="channel"), nullable=False)
    check_in_date: Mapped[date] = mapped_column(Date, nullable=False)
    check_out_date: Mapped[date] = mapped_column(Date, nullable=False)
    # JSON money values are decimal strings keyed by ISO stay date.
    nightly_bases: Mapped[dict[str, str]] = mapped_column(JSONB, nullable=False)
    total: Mapped[Decimal] = mapped_column(Numeric(10, 2), nullable=False)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    origin: Mapped[SourcePriceSnapshotOrigin] = mapped_column(
        PgEnum(SourcePriceSnapshotOrigin, name="source_price_snapshot_origin"), nullable=False
    )
    created_by: Mapped[str | None] = mapped_column(String(20), ForeignKey("users.user_id"))
    audit_log_id: Mapped[int | None] = mapped_column(ForeignKey("audit_logs.log_id"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    source_order = relationship("Order", foreign_keys=[source_order_id])


def _reject_snapshot_mutation(*_args, **_kwargs) -> None:
    raise ValueError("order source price snapshots are immutable")


def _validate_administrator_snapshot(
    _mapper, connection, target: OrderSourcePriceSnapshot
) -> None:
    action_by_origin = {
        SourcePriceSnapshotOrigin.administrator_fallback:
            "sponsorship.source_price_fallback",
        SourcePriceSnapshotOrigin.administrator_override:
            "sponsorship.source_price_override",
    }
    expected_action = action_by_origin.get(target.origin)
    if expected_action is None:
        return
    # Let the table check constraint give the canonical missing-audit failure.
    if target.created_by is None or target.audit_log_id is None:
        return
    audited_admin = connection.execute(
        select(User.user_id)
        .join(AuditLog, AuditLog.operator_id == User.user_id)
        .where(
            User.user_id == target.created_by,
            User.role == UserRole.admin,
            AuditLog.log_id == target.audit_log_id,
            AuditLog.action == expected_action,
            AuditLog.resource_type == "order",
            AuditLog.resource_id == target.source_order_id,
        )
    ).scalar_one_or_none()
    if audited_admin is None:
        raise ValueError("administrator source price requires a matching admin audit log")


def _validate_snapshot_consistency_and_version(
    _mapper, connection, target: OrderSourcePriceSnapshot
) -> None:
    source = connection.execute(
        select(Order.channel, Order.check_in_date, Order.check_out_date).where(
            Order.order_id == target.source_order_id,
            Order.is_deleted.is_(False),
        )
    ).one_or_none()
    if source is None:
        return  # The FK remains the canonical missing-source failure.
    if (
        source.channel != target.channel
        or source.check_in_date != target.check_in_date
        or source.check_out_date != target.check_out_date
    ):
        raise ValueError("source order and price snapshot must be consistent")

    expected_dates = {
        (target.check_in_date + timedelta(days=offset)).isoformat()
        for offset in range((target.check_out_date - target.check_in_date).days)
    }
    if not isinstance(target.nightly_bases, dict) or set(target.nightly_bases) != expected_dates:
        raise ValueError("source snapshot requires exact nightly decimal-string facts")
    try:
        amounts = [
            Decimal(value)
            for value in target.nightly_bases.values()
            if isinstance(value, str)
        ]
    except InvalidOperation as exc:
        raise ValueError("source snapshot requires exact nightly decimal-string facts") from exc
    if (
        len(amounts) != len(expected_dates)
        or any(not amount.is_finite() or amount < 0 for amount in amounts)
        or sum(amounts, Decimal("0.00")) != target.total
    ):
        raise ValueError("source snapshot requires exact nightly decimal-string facts")

    latest = connection.execute(
        select(func.max(OrderSourcePriceSnapshot.version)).where(
            OrderSourcePriceSnapshot.source_order_id == target.source_order_id
        )
    ).scalar_one()
    if target.version != (latest or 0) + 1:
        raise ValueError("source price snapshot version must append sequentially")

    if target.origin == SourcePriceSnapshotOrigin.administrator_fallback:
        upstream_exists = connection.execute(
            select(OrderSourcePriceSnapshot.source_price_snapshot_id)
            .where(
                OrderSourcePriceSnapshot.source_order_id == target.source_order_id,
                OrderSourcePriceSnapshot.origin.in_(
                    (
                        SourcePriceSnapshotOrigin.bypms_import,
                        SourcePriceSnapshotOrigin.bypms_adoption,
                    )
                ),
            )
            .limit(1)
        ).scalar_one_or_none()
        if upstream_exists is not None:
            raise ValueError("fallback is forbidden when a valid upstream snapshot exists")


event.listen(OrderSourcePriceSnapshot, "before_insert", _validate_snapshot_consistency_and_version)
event.listen(OrderSourcePriceSnapshot, "before_insert", _validate_administrator_snapshot)
event.listen(OrderSourcePriceSnapshot, "before_update", _reject_snapshot_mutation)
event.listen(OrderSourcePriceSnapshot, "before_delete", _reject_snapshot_mutation)
