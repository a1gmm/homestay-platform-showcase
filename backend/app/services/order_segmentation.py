"""Atomic conversion of one BYPMS zero-fee order into managed stay segments."""
from __future__ import annotations

from datetime import date, timedelta
from decimal import Decimal, InvalidOperation
from typing import Sequence

from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from app.core.ids import gen_order_room_id, unique_order_id
from app.models.audit_log import AuditLog
from app.models.managed_stay_group import ManagedStayGroup, ManagedStayGroupKind
from app.models.order import Order, OrderStatus, StaySettlementKind
from app.models.order_room import OrderRoom
from app.models.payment import Payment
from app.models.refund import Refund, RefundReason
from app.services.manual_override import (
    IdempotencyKeyReusedError,
    OperationInProgressError,
    claim_order_operation,
    complete_order_operation,
    lock_fields,
)
from app.services.room_availability import check_room_conflict
from app.services.sponsorship import (
    bind_company_sponsored_stay,
    calculate_sponsored_amount,
    default_channel_ratio,
    select_source_price_snapshot,
)
from app.services.stay_group import SettledStayMutationError, new_stay_group_id
from app.schemas.order import SplitSegment


class SplitStayError(RuntimeError):
    code = "INVALID_SEGMENT_COVERAGE"


class SplitCoverageError(SplitStayError):
    code = "INVALID_SEGMENT_COVERAGE"


class RoomConflictError(SplitStayError):
    code = "ROOM_OCCUPANCY_CONFLICT"


class StaleOrderVersionError(SplitStayError):
    code = "VERSION_CONFLICT"


class UnknownChannelRatioError(SplitStayError):
    code = "UNKNOWN_CHANNEL_RATIO"


class DuplicateSponsoredStayError(SplitStayError):
    code = "INVALID_SPONSORSHIP_TRANSITION"


class SourcePriceUnavailableError(SplitStayError):
    code = "SOURCE_PRICE_SNAPSHOT_MISSING"


class NotZeroFeeEligibleError(SplitStayError):
    code = "NOT_ZERO_FEE_ELIGIBLE"


class FinancialOverrideForbiddenError(SplitStayError):
    code = "FINANCIAL_OVERRIDE_FORBIDDEN"


def is_sponsorship_uniqueness_violation(error: IntegrityError) -> bool:
    """Recognize only duplicate company-sponsorship roots across supported DBs."""
    diagnostic = getattr(getattr(error, "orig", None), "diag", None)
    constraint_name = getattr(diagnostic, "constraint_name", None)
    if constraint_name in {
        "uq_company_sponsored_stays_active_identity",
        "company_sponsored_stays_segment_order_id_key",
    }:
        return True
    message = str(getattr(error, "orig", error)).lower()
    return (
        "unique constraint failed: company_sponsored_stays.segment_order_id" in message
        or (
            "unique constraint failed: company_sponsored_stays.source_order_id" in message
            and "company_sponsored_stays.segment_check_in_date" in message
            and "company_sponsored_stays.segment_check_out_date" in message
        )
    )


DOMAIN_ERROR_HTTP_STATUS = {
    "FINANCIAL_OVERRIDE_FORBIDDEN": 422,
    "VERSION_CONFLICT": 409,
    "IDEMPOTENCY_KEY_REUSED": 409,
    "ROOM_OCCUPANCY_CONFLICT": 409,
    "INVALID_SPONSORSHIP_TRANSITION": 409,
    "NOT_ZERO_FEE_ELIGIBLE": 422,
    "INVALID_SEGMENT_COVERAGE": 422,
    "SOURCE_PRICE_SNAPSHOT_MISSING": 422,
    "UNKNOWN_CHANNEL_RATIO": 422,
    "OPERATION_IN_PROGRESS": 409,
}


def domain_error_response(error: Exception) -> tuple[int, dict[str, str]]:
    """Single narrow HTTP adapter for segmentation and operation-domain errors."""
    code = getattr(error, "code", "INVALID_SPONSORSHIP_TRANSITION")
    if isinstance(error, IdempotencyKeyReusedError):
        code = "IDEMPOTENCY_KEY_REUSED"
    elif isinstance(error, OperationInProgressError):
        code = "OPERATION_IN_PROGRESS"
    return DOMAIN_ERROR_HTTP_STATUS.get(code, 422), {"code": code, "message": str(error)}


def validate_exact_coverage(
    source_start: date,
    source_end: date,
    segments: Sequence[SplitSegment],
) -> None:
    ordered = sorted(segments, key=lambda item: item.check_in_date)
    cursor = source_start
    for segment in ordered:
        if (
            segment.check_in_date != cursor
            or segment.check_out_date <= segment.check_in_date
        ):
            raise SplitCoverageError("住宿段必须连续、无重叠且完整覆盖原订单")
        cursor = segment.check_out_date
    if cursor != source_end:
        raise SplitCoverageError("住宿段未完整覆盖原订单")


def _request_payload(source_order_id: str, request) -> dict:
    return {
        "source_order_id": source_order_id,
        "expected_group_version": request.expected_group_version,
        "price_snapshot_id": request.price_snapshot_id,
        "segments": [
            {
                "check_in_date": segment.check_in_date.isoformat(),
                "check_out_date": segment.check_out_date.isoformat(),
                "room_id": segment.room_id,
                "settlement_kind": segment.settlement_kind.value,
            }
            for segment in sorted(
                request.segments,
                key=lambda item: (
                    item.check_in_date,
                    item.check_out_date,
                    item.room_id,
                    item.settlement_kind.value,
                ),
            )
        ],
    }


def _snapshot_base(snapshot, segment: SplitSegment) -> Decimal:
    total = Decimal("0.00")
    day = segment.check_in_date
    try:
        while day < segment.check_out_date:
            amount = Decimal(snapshot.nightly_bases[day.isoformat()])
            if not amount.is_finite() or amount < 0:
                raise InvalidOperation
            total += amount
            day += timedelta(days=1)
    except (InvalidOperation, KeyError, TypeError, ValueError) as exc:
        raise SourcePriceUnavailableError("所选宝寓价格快照缺少有效逐夜价格") from exc
    return total.quantize(Decimal("0.01"))


async def _validate_eligibility(db, source: Order) -> None:
    if source.is_deleted or not source.platform_order_id:
        raise NotZeroFeeEligibleError("仅可拆分有效的宝寓来源订单")
    if source.actual_price != Decimal("0.00"):
        raise NotZeroFeeEligibleError("订单房费必须为零")
    if source.stay_group_id:
        typed_group = await db.scalar(
            select(ManagedStayGroup).where(
                ManagedStayGroup.stay_group_id == source.stay_group_id
            )
        )
        if typed_group is None:
            raise NotZeroFeeEligibleError("普通续住组成员不能单独转换为 managed_split")
    if source.order_status in {
        OrderStatus.checked_in,
        OrderStatus.pending_checkout,
        OrderStatus.completed,
        OrderStatus.cancelled,
    }:
        raise SettledStayMutationError("已入住、已退房或已结算订单必须走更正流程")
    house_payments = await db.scalar(
        select(func.count(Payment.payment_id)).where(
            Payment.order_id == source.order_id,
            Payment.is_deleted.is_(False),
            Payment.is_deposit.is_(False),
        )
    )
    house_refunds = await db.scalar(
        select(func.count(Refund.refund_id)).where(
            Refund.order_id == source.order_id,
            Refund.is_deleted.is_(False),
            Refund.reason != RefundReason.deposit_return,
        )
    )
    if house_payments or house_refunds:
        raise NotZeroFeeEligibleError("订单已存在房费收款或退款")


async def split_eligibility(db, source: Order) -> tuple[bool, str | None, str | None]:
    """Return the read-only split gate used by the order manual-control projection."""
    try:
        await _validate_eligibility(db, source)
    except (NotZeroFeeEligibleError, SettledStayMutationError) as exc:
        return False, getattr(exc, "code", "INVALID_SPONSORSHIP_TRANSITION"), str(exc)

    aggregate = (
        await db.execute(
            select(ManagedStayGroup).where(
                ManagedStayGroup.source_order_id == source.order_id
            )
        )
    ).scalar_one_or_none()
    if aggregate is not None:
        return False, "ALREADY_MANAGED_SPLIT", "订单已完成住宿段拆分"

    snapshot = await select_source_price_snapshot(db, source.order_id)
    if snapshot is None:
        return False, "SOURCE_PRICE_SNAPSHOT_MISSING", "缺少可追溯的宝寓来源价格"
    expected_dates = {
        (source.check_in_date + timedelta(days=offset)).isoformat()
        for offset in range((source.check_out_date - source.check_in_date).days)
    }
    try:
        nightly = {
            stay_date: Decimal(str(amount))
            for stay_date, amount in snapshot.nightly_bases.items()
        }
        snapshot_is_valid = (
            snapshot.check_in_date == source.check_in_date
            and snapshot.check_out_date == source.check_out_date
            and set(nightly) == expected_dates
            and all(amount >= 0 for amount in nightly.values())
            and sum(nightly.values(), Decimal("0.00")) == snapshot.total
        )
    except (AttributeError, InvalidOperation, TypeError, ValueError):
        snapshot_is_valid = False
    if not snapshot_is_valid:
        return False, "SOURCE_PRICE_SNAPSHOT_INVALID", "来源价格快照不完整或不一致"
    if default_channel_ratio(source.channel) is None:
        return False, "UNKNOWN_CHANNEL_RATIO", "来源渠道没有已确认的公司承担比例"
    return True, None, None


async def _validate_rooms(db, source: Order, segments: Sequence[SplitSegment]) -> None:
    for segment in sorted(
        segments,
        key=lambda item: (item.room_id, item.check_in_date, item.check_out_date),
    ):
        if await check_room_conflict(
            db,
            segment.room_id,
            segment.check_in_date,
            segment.check_out_date,
            exclude_order_id=source.order_id,
            exclude_stay_group_id=source.stay_group_id,
        ):
            raise RoomConflictError(
                f"房间 {segment.room_id} 在目标日期已有占用或不可用"
            )


async def _load_snapshot_and_calculations(db, source: Order, request):
    snapshot = await select_source_price_snapshot(
        db, source.order_id, snapshot_id=request.price_snapshot_id
    )
    if snapshot is None:
        raise SourcePriceUnavailableError("所选宝寓价格快照不存在或不属于来源订单")
    calculations: dict[tuple[date, date, str], tuple[Decimal, Decimal, Decimal]] = {}
    for segment in request.segments:
        if segment.settlement_kind != StaySettlementKind.company_sponsored:
            continue
        ratio = default_channel_ratio(source.channel)
        if ratio is None:
            raise UnknownChannelRatioError("来源渠道没有已确认的公司承担比例")
        ratio = ratio.quantize(Decimal("0.0001"))
        base = _snapshot_base(snapshot, segment)
        amount = calculate_sponsored_amount(base, ratio)
        calculations[(segment.check_in_date, segment.check_out_date, segment.room_id)] = (
            base,
            ratio,
            amount,
        )
    return snapshot, calculations


async def preview_split(db, source_order: Order, request) -> dict:
    """Validate and derive an immutable-snapshot preview without writing rows."""
    source = (await db.execute(
        select(Order)
        .where(Order.order_id == source_order.order_id, Order.is_deleted.is_(False))
        .options(selectinload(Order.rooms))
    )).scalar_one_or_none()
    if source is None:
        raise NotZeroFeeEligibleError("来源订单不存在")
    existing = (await db.execute(
        select(ManagedStayGroup).where(ManagedStayGroup.source_order_id == source.order_id)
    )).scalar_one_or_none()
    current_version = existing.version if existing else 0
    if request.expected_group_version != current_version:
        raise StaleOrderVersionError("住宿组版本已变化")
    await _validate_eligibility(db, source)
    validate_exact_coverage(source.check_in_date, source.check_out_date, request.segments)
    await _validate_rooms(db, source, request.segments)
    _, calculations = await _load_snapshot_and_calculations(db, source, request)
    return {
        "stay_group_id": existing.stay_group_id if existing else None,
        "group_version": current_version,
        "segments": [
            {
                "order_id": None,
                "check_in_date": segment.check_in_date,
                "check_out_date": segment.check_out_date,
                "room_id": segment.room_id,
                "settlement_kind": segment.settlement_kind,
                "company_sponsored": (
                    {
                        "calculation_base": calculations[key][0],
                        "settlement_ratio": calculations[key][1],
                        "amount": calculations[key][2],
                    }
                    if (key := (
                        segment.check_in_date,
                        segment.check_out_date,
                        segment.room_id,
                    )) in calculations
                    else None
                ),
            }
            for segment in sorted(request.segments, key=lambda item: item.check_in_date)
        ],
    }


async def split_stay(
    db,
    source_order: Order,
    request,
    actor_id: str,
    *,
    operation_key: str,
) -> list[Order]:
    """Flush one complete managed split; the HTTP endpoint owns the sole commit."""
    source = (await db.execute(
        select(Order)
        .where(Order.order_id == source_order.order_id, Order.is_deleted.is_(False))
        .options(selectinload(Order.rooms))
        .with_for_update()
    )).scalar_one_or_none()
    if source is None:
        raise NotZeroFeeEligibleError("来源订单不存在")

    aggregate = (await db.execute(
        select(ManagedStayGroup)
        .where(ManagedStayGroup.source_order_id == source.order_id)
        .with_for_update()
    )).scalar_one_or_none()
    claim = await claim_order_operation(
        db,
        property_scope=f"source-order:{source.order_id}",
        operation="zero_fee_split",
        idempotency_key=operation_key,
        request_payload=_request_payload(source.order_id, request),
    )
    if claim.is_replay:
        return list((await db.execute(
            select(Order)
            .where(Order.order_id.in_(claim.operation.result_order_ids))
            .options(selectinload(Order.rooms), selectinload(Order.company_sponsorship))
            .order_by(Order.check_in_date, Order.order_id)
        )).scalars().all())

    current_version = aggregate.version if aggregate else 0
    if request.expected_group_version != current_version:
        raise StaleOrderVersionError("住宿组版本已变化")
    if aggregate is not None:
        raise StaleOrderVersionError("来源订单已经属于 managed_split")
    await _validate_eligibility(db, source)
    validate_exact_coverage(source.check_in_date, source.check_out_date, request.segments)
    await _validate_rooms(db, source, request.segments)
    _, calculations = await _load_snapshot_and_calculations(db, source, request)

    ordered = sorted(request.segments, key=lambda item: item.check_in_date)
    group_id = new_stay_group_id()
    aggregate = ManagedStayGroup(
        stay_group_id=group_id,
        source_order_id=source.order_id,
        kind=ManagedStayGroupKind.managed_split,
        version=1,
    )
    db.add(aggregate)
    await db.flush()

    original = {
        "order_id": source.order_id,
        "check_in_date": source.check_in_date.isoformat(),
        "check_out_date": source.check_out_date.isoformat(),
        "room_ids": [room.room_id for room in source.rooms],
    }
    await db.execute(delete(OrderRoom).where(OrderRoom.order_id == source.order_id))
    source.rooms = []
    results: list[Order] = []
    for position, draft in enumerate(ordered):
        if position == 0:
            segment_order = source
        else:
            segment_order = Order(
                order_id=await unique_order_id(db),
                channel=source.channel,
                platform_order_id=None,
                guest_name=source.guest_name,
                guest_phone=source.guest_phone,
                room_id=draft.room_id,
                check_in_date=draft.check_in_date,
                check_out_date=draft.check_out_date,
                list_price=Decimal("0.00"),
                discount_amount=Decimal("0.00"),
                actual_price=Decimal("0.00"),
                deposit=Decimal("0.00"),
                deposit_status=source.deposit_status,
                payment_status=source.payment_status,
                order_status=source.order_status,
                booking_type=source.booking_type,
                cleaning_status=source.cleaning_status,
                platform_commission_rate=Decimal("0.0000"),
                notes=source.notes,
                created_by=actor_id,
                is_deleted=False,
                metadata_=dict(source.metadata_ or {}),
            )
            db.add(segment_order)
        segment_order.room_id = draft.room_id
        segment_order.check_in_date = draft.check_in_date
        segment_order.check_out_date = draft.check_out_date
        segment_order.list_price = Decimal("0.00")
        segment_order.discount_amount = Decimal("0.00")
        segment_order.actual_price = Decimal("0.00")
        segment_order.platform_commission_rate = Decimal("0.0000")
        segment_order.stay_group_id = group_id
        segment_order.stay_settlement_kind = draft.settlement_kind
        segment_order.metadata_ = lock_fields(
            segment_order.metadata_,
            {
                "stay_structure",
                "check_in_date",
                "check_out_date",
                "room_assignment",
                "actual_price",
                "daily_prices",
                "ota_owner_revenue",
            },
        )
        db.add(OrderRoom(
            order_room_id=gen_order_room_id(),
            order_id=segment_order.order_id,
            room_id=draft.room_id,
            check_in_date=draft.check_in_date,
            check_out_date=draft.check_out_date,
            list_price=Decimal("0.00"),
            discount_amount=Decimal("0.00"),
            actual_price=Decimal("0.00"),
            guests_count=0,
            position=0,
            daily_prices={
                (draft.check_in_date + timedelta(days=offset)).isoformat(): "0.00"
                for offset in range((draft.check_out_date - draft.check_in_date).days)
            },
            metadata_={},
        ))
        results.append(segment_order)
    await db.flush()

    for segment_order, draft in zip(results, ordered):
        if draft.settlement_kind == StaySettlementKind.company_sponsored:
            try:
                await bind_company_sponsored_stay(
                    db,
                    source_order_id=source.order_id,
                    segment_order_id=segment_order.order_id,
                    source_price_snapshot_id=request.price_snapshot_id,
                    created_by=actor_id,
                )
            except IntegrityError as exc:
                if is_sponsorship_uniqueness_violation(exc):
                    raise DuplicateSponsoredStayError(
                        "住宿段已经绑定公司承担记录"
                    ) from exc
                raise
            except ValueError as exc:
                if "ratio" in str(exc):
                    raise UnknownChannelRatioError(str(exc)) from exc
                if "nightly" in str(exc) or "snapshot" in str(exc):
                    raise SourcePriceUnavailableError(str(exc)) from exc
                raise DuplicateSponsoredStayError(str(exc)) from exc

    db.add(AuditLog(
        operator_id=actor_id,
        action="order.zero_fee_split",
        resource_type="order",
        resource_id=source.order_id,
        before_data=original,
        after_data={
            "stay_group_id": group_id,
            "group_version": 1,
            "segments": [
                {
                    "order_id": order.order_id,
                    "check_in_date": draft.check_in_date.isoformat(),
                    "check_out_date": draft.check_out_date.isoformat(),
                    "room_id": draft.room_id,
                    "settlement_kind": draft.settlement_kind.value,
                }
                for order, draft in zip(results, ordered)
            ],
        },
        notes="split_free_stay",
    ))
    await complete_order_operation(
        db,
        claim.operation,
        result_order_ids=[order.order_id for order in results],
        result_stay_group_id=group_id,
    )
    return results
