"""Company-sponsored stay calculations and the sole transactional write API.

The BYPMS importer/adopter contract is deliberately small: in the same database
transaction that creates or adopts the ``Order``, call
``record_source_price_snapshot`` with the untouched upstream payload, explicit
origin and fetch time.  Callers own commit/rollback; this module locks the source
row, allocates versions, serializes JSON money as decimal strings and flushes.
The database migration mirrors the invariants for writers outside this process.
"""

from collections.abc import Mapping, Sequence
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal, InvalidOperation, ROUND_DOWN, ROUND_HALF_UP
from enum import Enum
import hashlib
import json
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.company_sponsored_stay import (
    CompanySponsoredStay,
    CompanySponsorshipStatus,
    PaymentResponsibility,
)
from app.models.company_sponsorship_adjustment import CompanySponsorshipAdjustment
from app.models.settlement import OwnerSettlementItem
from app.models.order import Channel, Order, StaySettlementKind
from app.models.order_source_price_snapshot import (
    OrderSourcePriceSnapshot,
    SourcePriceSnapshotOrigin,
)


MONEY_QUANTUM = Decimal("0.01")

_CHANNEL_RATIOS = {
    Channel.qunar: Decimal("0.75"),
    Channel.ctrip: Decimal("0.80"),
    Channel.zhixing: Decimal("0.80"),
    Channel.tongcheng: Decimal("0.80"),
}
_NIGHTLY_KEYS = (
    "nightlyPrices",
    "nightly_prices",
    "dailyPrices",
    "daily_prices",
    "priceByNight",
    "price_by_night",
)
_TOTAL_KEYS = ("originalTotal", "original_total", "priceFang", "roomTotal", "total")


class SponsorshipVersionConflictError(ValueError):
    code = "SPONSORSHIP_VERSION_CONFLICT"


class SponsorshipOperationKeyReusedError(ValueError):
    code = "IDEMPOTENCY_KEY_REUSED"


class SponsorshipNotCorrectableError(ValueError):
    code = "SPONSORSHIP_NOT_CORRECTABLE"


def default_channel_ratio(channel: Channel) -> Decimal | None:
    """Return an explicit contractual ratio; unknown channels are never guessed."""
    return _CHANNEL_RATIOS.get(channel)


def calculate_sponsored_amount(base: Decimal, ratio: Decimal) -> Decimal:
    """Calculate a sponsored amount using the project's two-decimal money rounding."""
    return (base * ratio).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value)[:10])
    except (TypeError, ValueError):
        return None


def _as_money(value: object) -> Decimal | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        amount = Decimal(str(value)).quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except (InvalidOperation, TypeError, ValueError):
        return None
    return amount if amount >= 0 else None


def _nightly_prices(
    raw: object, expected_dates: list[date]
) -> dict[date, Decimal] | None:
    parsed: dict[date, Decimal] = {}
    if isinstance(raw, Mapping):
        if len(raw) != len(expected_dates):
            return None
        for raw_date, raw_amount in raw.items():
            stay_date = _as_date(raw_date)
            amount = _as_money(raw_amount)
            if stay_date is None or amount is None or stay_date in parsed:
                return None
            parsed[stay_date] = amount
    elif isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
        if len(raw) != len(expected_dates):
            return None
        for position, item in enumerate(raw):
            if isinstance(item, Mapping):
                stay_date = next(
                    (
                        _as_date(item[key])
                        for key in ("date", "stayDate", "businessDate", "night", "day")
                        if key in item
                    ),
                    None,
                )
                amount = next(
                    (
                        _as_money(item[key])
                        for key in ("amount", "price", "originalPrice", "roomPrice", "priceFang", "base")
                        if key in item
                    ),
                    None,
                )
            else:
                stay_date = expected_dates[position] if position < len(expected_dates) else None
                amount = _as_money(item)
            if stay_date is None or amount is None or stay_date in parsed:
                return None
            parsed[stay_date] = amount
    else:
        return None

    if set(parsed) != set(expected_dates):
        return None
    return {stay_date: parsed[stay_date] for stay_date in expected_dates}


def derive_nightly_channel_bases(raw_payload: Mapping) -> dict[date, Decimal] | None:
    """Derive immutable nightly channel bases from one upstream price payload.

    Explicit upstream nightly prices win. When only a total is available, equal
    two-decimal nightly amounts are used and the final night absorbs the remainder.
    Invalid or incomplete inputs return ``None`` rather than inventing a price.
    """
    check_in = _as_date(
        raw_payload.get("checkIn", raw_payload.get("check_in", raw_payload.get("check_in_date")))
    )
    check_out = _as_date(
        raw_payload.get("checkOut", raw_payload.get("check_out", raw_payload.get("check_out_date")))
    )
    if check_in is None or check_out is None or check_out <= check_in:
        return None

    nights = (check_out - check_in).days
    expected_dates = [check_in + timedelta(days=offset) for offset in range(nights)]
    for key in _NIGHTLY_KEYS:
        if key in raw_payload:
            return _nightly_prices(raw_payload[key], expected_dates)

    total = next(
        (_as_money(raw_payload[key]) for key in _TOTAL_KEYS if key in raw_payload),
        None,
    )
    if total is None:
        return None
    equal_night = (total / nights).quantize(MONEY_QUANTUM, rounding=ROUND_DOWN)
    amounts = [equal_night] * (nights - 1)
    amounts.append(total - sum(amounts, Decimal("0.00")))
    return dict(zip(expected_dates, amounts, strict=True))


def _canonical_json_value(value: object) -> object:
    if isinstance(value, Mapping):
        return {
            str(key): _canonical_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_canonical_json_value(item) for item in value]
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, Enum):
        return value.value
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    return str(value)


def _payload_hash(raw_payload: Mapping) -> str:
    encoded = json.dumps(
        _canonical_json_value(raw_payload),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _money_string(value: Decimal) -> str:
    return format(value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP), ".2f")


async def record_source_price_snapshot(
    db: AsyncSession,
    *,
    source_order_id: str,
    raw_payload: Mapping,
    origin: SourcePriceSnapshotOrigin,
    fetched_at: datetime,
    created_by: str | None = None,
    audit_log_id: int | None = None,
) -> OrderSourcePriceSnapshot:
    """Idempotently append an immutable source-price version under a row lock."""
    origin = SourcePriceSnapshotOrigin(origin)
    source = (
        await db.execute(
            select(Order)
            .where(Order.order_id == source_order_id, Order.is_deleted.is_(False))
            .with_for_update()
        )
    ).scalar_one_or_none()
    if source is None:
        raise ValueError("source order does not exist")

    bases = derive_nightly_channel_bases(raw_payload)
    if bases is None:
        raise ValueError("source price payload has no valid nightly bases")
    check_in = min(bases)
    check_out = max(bases) + timedelta(days=1)
    if (check_in, check_out) != (source.check_in_date, source.check_out_date):
        raise ValueError("source price snapshot dates must be consistent with source order")

    if origin == SourcePriceSnapshotOrigin.administrator_fallback:
        upstream_exists = await db.scalar(
            select(func.count(OrderSourcePriceSnapshot.source_price_snapshot_id)).where(
                OrderSourcePriceSnapshot.source_order_id == source_order_id,
                OrderSourcePriceSnapshot.origin.in_(
                    (
                        SourcePriceSnapshotOrigin.bypms_import,
                        SourcePriceSnapshotOrigin.bypms_adoption,
                    )
                ),
            )
        )
        if upstream_exists:
            raise ValueError("fallback is forbidden when a valid upstream snapshot exists")

    payload_hash = _payload_hash(raw_payload)
    existing = (
        await db.execute(
            select(OrderSourcePriceSnapshot).where(
                OrderSourcePriceSnapshot.source_order_id == source_order_id,
                OrderSourcePriceSnapshot.upstream_payload_hash == payload_hash,
                OrderSourcePriceSnapshot.origin == origin,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        return existing

    latest_version = await db.scalar(
        select(func.max(OrderSourcePriceSnapshot.version)).where(
            OrderSourcePriceSnapshot.source_order_id == source_order_id
        )
    )
    snapshot = OrderSourcePriceSnapshot(
        source_price_snapshot_id=f"SPS-{uuid4().hex}",
        source_order_id=source_order_id,
        version=(latest_version or 0) + 1,
        upstream_payload_hash=payload_hash,
        channel=source.channel,
        check_in_date=check_in,
        check_out_date=check_out,
        nightly_bases={day.isoformat(): _money_string(amount) for day, amount in bases.items()},
        total=sum(bases.values(), Decimal("0.00")),
        fetched_at=fetched_at,
        origin=origin,
        created_by=created_by,
        audit_log_id=audit_log_id,
    )
    db.add(snapshot)
    await db.flush()
    return snapshot


async def select_source_price_snapshot(
    db: AsyncSession,
    source_order_id: str,
    *,
    snapshot_id: str | None = None,
    for_update: bool = False,
) -> OrderSourcePriceSnapshot | None:
    """Select a bound version, or the latest version for one source order."""
    statement = select(OrderSourcePriceSnapshot).where(
        OrderSourcePriceSnapshot.source_order_id == source_order_id
    )
    if snapshot_id is not None:
        statement = statement.where(
            OrderSourcePriceSnapshot.source_price_snapshot_id == snapshot_id
        )
    else:
        statement = statement.order_by(OrderSourcePriceSnapshot.version.desc()).limit(1)
    if for_update:
        statement = statement.with_for_update()
    return (await db.execute(statement)).scalar_one_or_none()


async def bind_company_sponsored_stay(
    db: AsyncSession,
    *,
    source_order_id: str,
    segment_order_id: str,
    source_price_snapshot_id: str,
    settlement_ratio: Decimal | None = None,
    payment_responsibility: PaymentResponsibility = PaymentResponsibility.company_payable,
    created_by: str | None = None,
) -> CompanySponsoredStay:
    """Bind a sponsored segment to an immutable price version and derive its amount."""
    locked_orders = (
        await db.execute(
            select(Order)
            .where(
                Order.order_id.in_(sorted((source_order_id, segment_order_id))),
                Order.is_deleted.is_(False),
            )
            .order_by(Order.order_id)
            .with_for_update()
        )
    ).scalars().all()
    orders = {order.order_id: order for order in locked_orders}
    source = orders.get(source_order_id)
    segment = orders.get(segment_order_id)
    if source is None or segment is None:
        raise ValueError("source and segment orders must exist")
    if segment.stay_settlement_kind != StaySettlementKind.company_sponsored:
        raise ValueError("segment order must be typed company_sponsored")

    snapshot = await select_source_price_snapshot(
        db,
        source_order_id,
        snapshot_id=source_price_snapshot_id,
        for_update=True,
    )
    if snapshot is None:
        raise ValueError("selected source price snapshot does not belong to source order")
    if snapshot.channel != source.channel:
        raise ValueError("source, snapshot and sponsorship channel must be consistent")
    if (
        segment.check_in_date < snapshot.check_in_date
        or segment.check_out_date > snapshot.check_out_date
    ):
        raise ValueError("segment dates must fall within the selected source snapshot")

    segment_dates = [
        segment.check_in_date + timedelta(days=offset)
        for offset in range((segment.check_out_date - segment.check_in_date).days)
    ]
    try:
        calculation_base = sum(
            (Decimal(snapshot.nightly_bases[day.isoformat()]) for day in segment_dates),
            Decimal("0.00"),
        ).quantize(MONEY_QUANTUM)
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise ValueError("selected source price snapshot lacks valid segment nightly bases") from exc

    ratio = settlement_ratio if settlement_ratio is not None else default_channel_ratio(source.channel)
    if ratio is None:
        raise ValueError("source channel has no configured sponsorship ratio")
    ratio = Decimal(str(ratio)).quantize(Decimal("0.0001"))
    if not Decimal("0") <= ratio <= Decimal("1"):
        raise ValueError("settlement ratio must be between zero and one")

    root = CompanySponsoredStay(
        sponsored_stay_id=f"CSS-{uuid4().hex}",
        source_order_id=source_order_id,
        segment_order_id=segment_order_id,
        segment_check_in_date=segment.check_in_date,
        segment_check_out_date=segment.check_out_date,
        channel=source.channel,
        calculation_base=calculation_base,
        settlement_ratio=ratio,
        amount=calculate_sponsored_amount(calculation_base, ratio),
        payment_responsibility=payment_responsibility,
        status=CompanySponsorshipStatus.confirmed,
        source_price_snapshot_id=snapshot.source_price_snapshot_id,
        created_by=created_by,
    )
    db.add(root)
    await db.flush()
    return root


async def append_sponsorship_adjustment(
    db: AsyncSession,
    *,
    sponsorship_id: str,
    delta: Decimal,
    reason: str,
    operation_key: str,
    actor_id: str | None = None,
    system_principal: str | None = None,
    expected_version: int | None = None,
) -> CompanySponsorshipAdjustment:
    """Append one audited correction; repeat operation keys are idempotent."""
    reason = reason.strip()
    operation_key = operation_key.strip()
    actor_id = (actor_id.strip() or None) if actor_id is not None else None
    system_principal = (
        (system_principal.strip() or None) if system_principal is not None else None
    )
    if not reason or not operation_key:
        raise ValueError("adjustment reason and operation key must be nonblank")
    if (actor_id is None) == (system_principal is None):
        raise ValueError(
            "adjustment requires exactly one nonblank actor or system principal"
        )
    try:
        delta = Decimal(str(delta))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ValueError("adjustment delta must be finite") from exc
    if not delta.is_finite():
        raise ValueError("adjustment delta must be finite")
    try:
        delta = delta.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)
    except InvalidOperation as exc:
        raise ValueError("adjustment delta must be finite") from exc
    if delta == 0:
        raise ValueError("adjustment delta must be nonzero")

    root = (
        await db.execute(
            select(CompanySponsoredStay)
            .where(CompanySponsoredStay.sponsored_stay_id == sponsorship_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if root is None:
        raise ValueError("company sponsorship root does not exist")
    existing = (
        await db.execute(
            select(CompanySponsorshipAdjustment).where(
                CompanySponsorshipAdjustment.sponsorship_id == sponsorship_id,
                CompanySponsorshipAdjustment.operation_key == operation_key,
            )
        )
    ).scalar_one_or_none()
    if existing is not None:
        if (
            existing.delta != delta
            or existing.reason != reason
            or existing.actor_id != actor_id
            or existing.system_principal != system_principal
        ):
            raise SponsorshipOperationKeyReusedError(
                "adjustment operation key already has different facts"
            )
        return existing
    if root.status == CompanySponsorshipStatus.voided:
        raise SponsorshipNotCorrectableError("voided sponsorship cannot be corrected")
    if expected_version is not None and root.version != expected_version:
        raise SponsorshipVersionConflictError(
            f"sponsorship version changed: expected {expected_version}, current {root.version}"
        )

    adjustment = CompanySponsorshipAdjustment(
        adjustment_id=f"CSA-{uuid4().hex}",
        sponsorship_id=sponsorship_id,
        delta=delta,
        reason=reason,
        operation_key=operation_key,
        actor_id=actor_id,
        system_principal=system_principal,
        created_at=datetime.now(UTC),
    )
    db.add(adjustment)
    root.version += 1
    await db.flush()
    if root.status == CompanySponsorshipStatus.settled:
        original_item = await db.get(
            OwnerSettlementItem, root.settlement_item_id, with_for_update=True
        )
        if original_item is None or original_item.settlement_id != root.settlement_batch_id:
            raise ValueError("settled sponsorship requires its original settlement item")
        externally_settled = (
            root.payment_responsibility == PaymentResponsibility.channel_settled
        )
        owner_delta = (delta * Decimal(original_item.share_ratio_snapshot)).quantize(
            MONEY_QUANTUM
        )
        db.add(
            OwnerSettlementItem(
                item_id=f"SLI-{uuid4().hex[:12].upper()}",
                settlement_id=original_item.settlement_id,
                room_id=original_item.room_id,
                label="公司承担修正",
                order_count=0,
                revenue=Decimal("0.00"),
                commission=Decimal("0.00"),
                net_revenue=delta,
                externally_settled_income=(
                    delta if externally_settled else Decimal("0.00")
                ),
                sponsorship_adjustment_id=adjustment.adjustment_id,
                owner_expenses=Decimal("0.00"),
                share_ratio_snapshot=original_item.share_ratio_snapshot,
                owner_net_amount=(Decimal("0.00") if externally_settled else owner_delta),
                cost_share_breakdown=[
                    {
                        "source": "company_sponsorship_adjustment",
                        "type": "company_sponsorship_adjustment",
                        "sponsorship_id": root.sponsored_stay_id,
                        "adjustment_id": adjustment.adjustment_id,
                        "delta": str(delta),
                        "reason": reason,
                        "payment_responsibility": root.payment_responsibility.value,
                        "externally_settled": externally_settled,
                    }
                ],
            )
        )
        await db.flush()
    return adjustment
