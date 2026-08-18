from fastapi import APIRouter, BackgroundTasks, Header, HTTPException, Query
from pydantic import BaseModel, ValidationError
from sqlalchemy import select, and_, or_, func, delete, tuple_
from sqlalchemy.orm import selectinload, aliased
from datetime import date, datetime, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional
import logging
import uuid

from app.core.deps import DBSession, CurrentUser
from app.core.config import settings
from app.core.datetime_helpers import today_cn
from app.models.order import Order, OrderStatus, Channel, DepositStatus, PaymentStatus, BookingType, order_status_label, OTA_PLATFORM_CHANNELS
from app.models.order_room import OrderRoom
from app.models.managed_stay_group import ManagedStayGroup
from app.models.company_sponsored_stay import CompanySponsoredStay
from app.models.order_sync_conflict import OrderSyncConflict, OrderSyncConflictStatus
from app.models.order_source_price_snapshot import SourcePriceSnapshotOrigin
from app.models.audit_log import AuditLog
from app.models.payment import Payment
from app.models.room import Room, RoomStatus
from app.models.task import Task, TaskType, TaskStatus, TaskPriority
from app.schemas.order import (
    OrderCreate, OrderUpdate, OrderOut, OrderListItem, OrderRoomCreate,
    StayGroupOut, StaySegmentDetail, LinkCandidateOut, PaginatedSegmentList,
    PaginatedOrderList, DailyPriceUpdate, TransferRoomRequest, TransferReason,
    SwapRoomsRequest,
    ManualOverrideUpdate,
    ZeroFeeSplitOut, ZeroFeeSplitRequest,
    OrderManualControlOut, SourcePriceSnapshotOut,
    SourcePriceSnapshotAdminOverrideRequest,
    SyncConflictDecisionRequest, SyncConflictDecisionOut,
    SponsorshipCorrectionRequest, SponsorshipCorrectionResponse,
    normal_booking_violation,
    owner_revenue_requires_actual_violation, OWNER_REVENUE_REQUIRES_ACTUAL,
)
from app.services.room_availability import check_room_conflict
from app.services.audit import log_action, log_action_tx
from app.services.guest_service import link_guest_to_order
from app.services.order_pricing import (
    compute_daily_prices, sum_daily_prices, sync_ota_commission_rate,
    clear_per_room_owner_revenue, safe_decimal,
)
from app.services.order_transfer import apply_room_transfer
from app.services.payment_service import sum_house_fee_paid
from app.services.manual_override import apply_snapshot_manual_locks, lock_fields, locked_fields, unlock_fields
from app.services.order_segmentation import (
    FinancialOverrideForbiddenError,
    SettledStayMutationError,
    SplitStayError,
    domain_error_response,
    preview_split,
    split_eligibility,
    split_stay,
)
from app.services.manual_override import IdempotencyKeyReusedError, OperationInProgressError
from app.services.sponsorship import (
    SponsorshipNotCorrectableError,
    SponsorshipOperationKeyReusedError,
    SponsorshipVersionConflictError,
    append_sponsorship_adjustment,
    record_source_price_snapshot,
    select_source_price_snapshot,
)

router = APIRouter(prefix="/orders", tags=["orders"])

TERMINAL_STATUSES = {OrderStatus.completed, OrderStatus.cancelled}


def _require_canonical_lock_writer() -> None:
    if not settings.PMS_CANONICAL_LOCK_WRITE_ENABLED:
        raise HTTPException(
            503,
            detail={
                "code": "PMS_CANONICAL_LOCK_WRITE_DISABLED",
                "message": "人工接管写入暂未开放",
            },
        )


def _require_split_enabled() -> None:
    if not (
        settings.BYPMS_SPLIT_STAY_ENABLED
        and settings.PMS_CANONICAL_LOCK_WRITE_ENABLED
    ):
        raise HTTPException(
            503,
            detail={
                "code": "BYPMS_SPLIT_STAY_DISABLED",
                "message": "拆分住宿段功能暂未开放",
            },
        )


# ─── Audit snapshot helper ────────────────────────────────────────────────────
#
# 操作日志卡片化（PR-3b）需要每条 audit 记录都附一份完整订单快照，前端按截图样
# 式渲染"渠道/订单类型/预订人/手机号/房间/入住时间/房费/备注"全字段卡片。
# 写入路径上仍用 after_data 作为 snapshot 载体，老的 diff-only 数据由前端兜底
# 降级为简版 timeline。
def order_snapshot(order: Order) -> dict:
    """把订单序列化成审计快照（JSONB 友好，所有 Decimal/date/enum 转 str）。
    多房订单 rooms 字段保留 room_id + 日期 + 实收，足够卡片展示。
    rooms 未 eager-load 时安全降级为 []，调用方应尽量先 selectinload。"""
    if order is None:
        return {}
    from sqlalchemy import inspect as _sa_inspect
    rooms_payload = []
    try:
        state = _sa_inspect(order)
        if "rooms" not in state.unloaded:
            for r in (order.rooms or []):
                rooms_payload.append({
                    "order_room_id": r.order_room_id,
                    "room_id": r.room_id,
                    "check_in_date": str(r.check_in_date) if r.check_in_date else None,
                    "check_out_date": str(r.check_out_date) if r.check_out_date else None,
                    "actual_price": str(r.actual_price) if r.actual_price is not None else None,
                    "guests_count": r.guests_count,
                })
    except Exception:
        # 任何检查失败都不阻塞 audit 写入
        rooms_payload = []
    return {
        "order_id": order.order_id,
        "status": order.order_status.value if order.order_status else None,
        "booking_type": order.booking_type.value if getattr(order, "booking_type", None) else "normal",
        "channel": order.channel.value if order.channel else None,
        "guest_name": order.guest_name,
        "guest_phone": order.guest_phone,
        "room_id": order.room_id,
        "rooms": rooms_payload,
        "check_in_date": str(order.check_in_date) if order.check_in_date else None,
        "check_out_date": str(order.check_out_date) if order.check_out_date else None,
        "nights": order.nights,
        "actual_price": str(order.actual_price) if order.actual_price is not None else None,
        "ota_owner_revenue": (
            str(order.expected_revenue) if order.expected_revenue is not None else None
        ),
        "list_price": str(order.list_price) if order.list_price is not None else None,
        "deposit": str(order.deposit) if order.deposit is not None else None,
        "deposit_status": order.deposit_status.value if order.deposit_status else None,
        "deposit_returned": str(order.deposit_returned) if order.deposit_returned is not None else None,
        "stay_group_id": order.stay_group_id,
        "notes": order.notes,
        "note": order.notes,
        "manual_override_fields": order.manual_override_fields,
    }


# ─── RBAC helpers ────────────────────────────────────────────────────────────

def assert_can_write(current_user: dict):
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="无权操作订单")


_SPLIT_FINANCE_FIELDS = frozenset({
    "base", "base_amount", "calculation_base", "ratio", "settlement_ratio",
    "amount", "sponsored_amount", "final_amount", "responsibility",
    "payment_responsibility",
})

_MANAGED_STRUCTURAL_FIELDS = frozenset({
    "check_in_date", "check_out_date", "room_assignment", "stay_structure",
    "actual_price", "daily_prices", "ota_owner_revenue", "channel",
})


def _parse_zero_fee_split_body(payload: dict) -> ZeroFeeSplitRequest:
    supplied = set(payload) & _SPLIT_FINANCE_FIELDS
    for segment in payload.get("segments", ()) if isinstance(payload, dict) else ():
        if isinstance(segment, dict):
            supplied.update(set(segment) & _SPLIT_FINANCE_FIELDS)
    if supplied:
        raise FinancialOverrideForbiddenError(
            "拆分金额、计算基数和渠道比例只能由所选价格快照在服务端计算"
        )
    try:
        return ZeroFeeSplitRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=422, detail=exc.errors(include_url=False)) from exc


def _parse_source_price_override_body(
    payload: dict,
) -> SourcePriceSnapshotAdminOverrideRequest:
    total = payload.get("total") if isinstance(payload, dict) else None
    nightly = payload.get("nightly_prices") if isinstance(payload, dict) else None
    has_non_string_money = (
        total is not None and not isinstance(total, str)
    ) or (
        isinstance(nightly, dict)
        and any(not isinstance(value, str) for value in nightly.values())
    )
    if has_non_string_money:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "MONEY_MUST_BE_DECIMAL_STRING",
                "message": "金额必须使用十进制字符串",
            },
        )
    try:
        return SourcePriceSnapshotAdminOverrideRequest.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(
            status_code=422,
            detail={
                "code": "SOURCE_PRICE_OVERRIDE_INVALID",
                "message": "来源价格更正内容不完整或格式不正确",
                "errors": exc.errors(include_url=False, include_context=False),
            },
        ) from exc


def _raise_split_domain_http(error: Exception) -> None:
    status_code, detail = domain_error_response(error)
    raise HTTPException(status_code=status_code, detail=detail) from error


async def _reject_managed_ordinary_mutation(db, *orders: Order) -> None:
    from app.services.stay_group import reject_managed_ordinary_mutation

    try:
        await reject_managed_ordinary_mutation(db, *orders)
    except SettledStayMutationError as exc:
        await db.rollback()
        _raise_split_domain_http(exc)


async def _split_response(db, orders: list[Order], *, version: int, group_id: str | None):
    loaded = list((await db.execute(
        select(Order)
        .where(Order.order_id.in_([order.order_id for order in orders]))
        .options(selectinload(Order.company_sponsorship))
        .execution_options(populate_existing=True)
        .order_by(Order.check_in_date, Order.order_id)
    )).scalars().all())
    room_rows = (await db.execute(
        select(OrderRoom.order_id, OrderRoom.room_id)
        .where(OrderRoom.order_id.in_([order.order_id for order in loaded]))
    )).all()
    room_by_order = {order_id: room_id for order_id, room_id in room_rows}
    return ZeroFeeSplitOut(
        stay_group_id=group_id,
        group_version=version,
        segments=[
            {
                "order_id": order.order_id,
                "check_in_date": order.check_in_date,
                "check_out_date": order.check_out_date,
                "room_id": room_by_order[order.order_id],
                "settlement_kind": order.stay_settlement_kind,
                "company_sponsored": (
                    {
                        "calculation_base": order.company_sponsorship.calculation_base,
                        "settlement_ratio": order.company_sponsorship.settlement_ratio,
                        "amount": order.company_sponsorship.amount,
                    }
                    if order.company_sponsorship is not None else None
                ),
            }
            for order in loaded
        ],
    )


def _manual_control_value(order: Order, field: str):
    room_ids = [room.room_id for room in order.rooms if room.room_id]
    values = {
        "guest_name": order.guest_name,
        "guest_profile": {"guest_phone": order.guest_phone},
        "check_in_date": order.check_in_date.isoformat(),
        "check_out_date": order.check_out_date.isoformat(),
        "room_assignment": room_ids,
        "stay_structure": order.stay_group_id,
        "actual_price": str(order.actual_price) if order.actual_price is not None else None,
        "daily_prices": {
            room.order_room_id: dict(room.daily_prices or {}) for room in order.rooms
        },
        "ota_owner_revenue": (
            str(order.expected_revenue) if order.expected_revenue is not None else None
        ),
        "channel": order.channel.value,
        "note": order.notes,
        "order_status": order.order_status.value,
    }
    return values.get(field)


async def _manual_control_source(db, order: Order) -> tuple[Order, ManagedStayGroup | None]:
    aggregate = None
    if order.stay_group_id:
        aggregate = await db.get(ManagedStayGroup, order.stay_group_id)
    if aggregate is None:
        return order, None
    source = (
        await db.execute(
            select(Order)
            .where(Order.order_id == aggregate.source_order_id, Order.is_deleted == False)
            .options(selectinload(Order.rooms))
        )
    ).scalar_one()
    return source, aggregate


def _correction_response(root: CompanySponsoredStay) -> SponsorshipCorrectionResponse:
    return SponsorshipCorrectionResponse(
        original=root,
        corrections=list(root.adjustments),
        current_effective_amount=root.effective_amount,
        version=root.version,
    )


@router.post(
    "/{order_id}/sponsorship-corrections",
    response_model=SponsorshipCorrectionResponse,
)
async def append_company_sponsorship_correction(
    order_id: str,
    body: SponsorshipCorrectionRequest,
    db: DBSession,
    current_user: CurrentUser,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Append an administrator-audited immutable sponsorship delta."""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可修正公司承担金额")
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            status_code=400,
            detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "缺少 Idempotency-Key"},
        )

    root = (
        await db.execute(
            select(CompanySponsoredStay).where(
                CompanySponsoredStay.segment_order_id == order_id
            )
        )
    ).scalar_one_or_none()
    if root is None:
        raise HTTPException(
            status_code=404,
            detail={"code": "SPONSORSHIP_NOT_FOUND", "message": "公司承担记录不存在"},
        )

    try:
        adjustment = await append_sponsorship_adjustment(
            db,
            sponsorship_id=root.sponsored_stay_id,
            delta=body.delta,
            reason=body.reason,
            operation_key=idempotency_key.strip(),
            actor_id=current_user["user_id"],
            expected_version=body.expected_version,
        )
        loaded = (
            await db.execute(
                select(CompanySponsoredStay)
                .where(
                    CompanySponsoredStay.sponsored_stay_id == root.sponsored_stay_id
                )
                .options(selectinload(CompanySponsoredStay.adjustments))
                .execution_options(populate_existing=True)
            )
        ).scalar_one()
        response = _correction_response(loaded)
        prior_audit_payloads = (
            await db.execute(
                select(AuditLog.after_data).where(
                    AuditLog.action == "sponsorship.correct",
                    AuditLog.resource_type == "company_sponsored_stay",
                    AuditLog.resource_id == loaded.sponsored_stay_id,
                )
            )
        ).scalars().all()
        already_audited = any(
            (payload or {}).get("operation_key") == adjustment.operation_key
            for payload in prior_audit_payloads
        )
        if not already_audited:
            await log_action_tx(
                db,
                current_user["user_id"],
                "sponsorship.correct",
                "company_sponsored_stay",
                loaded.sponsored_stay_id,
                after_data={
                    "adjustment_id": adjustment.adjustment_id,
                    "delta": str(adjustment.delta),
                    "reason": adjustment.reason,
                    "operation_key": adjustment.operation_key,
                    "version": loaded.version,
                    "effective_amount": str(loaded.effective_amount),
                },
            )
        await db.commit()
        return response
    except (
        SponsorshipVersionConflictError,
        SponsorshipOperationKeyReusedError,
        SponsorshipNotCorrectableError,
    ) as exc:
        await db.rollback()
        raise HTTPException(
            status_code=409,
            detail={"code": exc.code, "message": str(exc)},
        ) from exc
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=422,
            detail={"code": "INVALID_SPONSORSHIP_CORRECTION", "message": str(exc)},
        ) from exc


@router.get("/{order_id}/manual-control", response_model=OrderManualControlOut)
async def get_order_manual_control(
    order_id: str,
    db: DBSession,
    current_user: CurrentUser,
):
    order = (
        await db.execute(
            select(Order)
            .where(Order.order_id == order_id, Order.is_deleted == False)
            .options(selectinload(Order.rooms))
        )
    ).scalar_one_or_none()
    if order is None:
        raise HTTPException(
            404, detail={"code": "ORDER_NOT_FOUND", "message": "订单不存在"}
        )

    source, aggregate = await _manual_control_source(db, order)
    conflicts = list(
        (
            await db.execute(
                select(OrderSyncConflict)
                .where(
                    OrderSyncConflict.source_order_id == source.order_id,
                    OrderSyncConflict.status == OrderSyncConflictStatus.open,
                )
                .order_by(
                    OrderSyncConflict.first_seen_at,
                    OrderSyncConflict.conflict_id,
                )
            )
        ).scalars()
    )
    ownership = sorted(locked_fields(source.metadata_))
    snapshot = await select_source_price_snapshot(db, source.order_id)
    split_enabled = bool(
        settings.BYPMS_SPLIT_STAY_ENABLED
        and settings.PMS_CANONICAL_LOCK_WRITE_ENABLED
    )
    split_candidate = bool(
        source.platform_order_id
        and source.actual_price == Decimal("0.00")
        and aggregate is None
    )
    split_visible = split_enabled and split_candidate
    is_relevant = bool(
        split_candidate
        or aggregate is not None
        or ownership
        or conflicts
    )
    eligible = False
    blocker_code = None
    blocker_message = None
    if not split_enabled and split_candidate:
        blocker_code = "FEATURE_DISABLED"
        blocker_message = "拆分住宿段功能暂未开放"
    elif split_visible:
        eligible, blocker_code, blocker_message = await split_eligibility(db, source)

    return {
        "source_order_id": source.order_id,
        "is_relevant": is_relevant,
        "split": {
            "enabled": split_enabled,
            "visible": split_visible,
            "eligible": eligible,
            "blocker_code": blocker_code,
            "blocker_message": blocker_message,
            "group_version": aggregate.version if aggregate is not None else 0,
        },
        "source_price_snapshot": snapshot,
        "locked_fields": [
            {
                "field": field,
                "current_value": _manual_control_value(source, field),
            }
            for field in ownership
        ],
        "open_conflicts": [
            {
                "conflict_id": conflict.conflict_id,
                "field": conflict.field,
                "local_value": conflict.local_value,
                "upstream_value": conflict.upstream_value,
                "upstream_version": conflict.upstream_version,
                "first_seen_at": conflict.first_seen_at,
                "last_seen_at": conflict.last_seen_at,
                "can_restore_following": not (
                    aggregate is not None
                    and conflict.field in _MANAGED_STRUCTURAL_FIELDS
                ),
            }
            for conflict in conflicts
        ],
        "can_administer": current_user["role"] == "admin",
        "can_write": current_user["role"] in ("admin", "operator"),
    }


@router.post(
    "/{order_id}/source-price-snapshots/admin-override",
    response_model=SourcePriceSnapshotOut,
)
async def create_source_price_snapshot_admin_override(
    order_id: str,
    body: dict,
    db: DBSession,
    current_user: CurrentUser,
):
    if current_user["role"] != "admin":
        raise HTTPException(
            403,
            detail={
                "code": "SOURCE_PRICE_OVERRIDE_FORBIDDEN",
                "message": "仅管理员可更正来源价格",
            },
        )
    _require_split_enabled()
    request = _parse_source_price_override_body(body)
    source = (
        await db.execute(
            select(Order)
            .where(Order.order_id == order_id, Order.is_deleted == False)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(
            404, detail={"code": "ORDER_NOT_FOUND", "message": "订单不存在"}
        )
    eligible, blocker_code, blocker_message = await split_eligibility(db, source)
    if not eligible and blocker_code not in {
        "SOURCE_PRICE_SNAPSHOT_MISSING",
        "SOURCE_PRICE_SNAPSHOT_INVALID",
    }:
        raise HTTPException(
            422,
            detail={
                "code": "SOURCE_PRICE_OVERRIDE_NOT_ELIGIBLE",
                "message": blocker_message or "仅可更正待拆分的宝寓零房费订单来源价格",
            },
        )
    latest = await select_source_price_snapshot(db, source.order_id)
    latest_id = latest.source_price_snapshot_id if latest is not None else None
    if request.based_on_snapshot_id != latest_id:
        raise HTTPException(
            409,
            detail={
                "code": "VERSION_CONFLICT",
                "message": "来源价格已变化，请刷新后重新预览",
            },
        )

    origin = (
        SourcePriceSnapshotOrigin.administrator_override
        if latest is not None
        else SourcePriceSnapshotOrigin.administrator_fallback
    )
    action = (
        "sponsorship.source_price_override"
        if latest is not None
        else "sponsorship.source_price_fallback"
    )
    audit = AuditLog(
        operator_id=current_user["user_id"],
        action=action,
        resource_type="order",
        resource_id=source.order_id,
        before_data=(
            {
                "source_price_snapshot_id": latest.source_price_snapshot_id,
                "version": latest.version,
                "nightly_bases": latest.nightly_bases,
                "total": str(latest.total),
            }
            if latest is not None
            else None
        ),
        notes=request.reason,
    )
    db.add(audit)
    await db.flush()
    raw_payload = {
        "checkIn": source.check_in_date.isoformat(),
        "checkOut": source.check_out_date.isoformat(),
        "basedOnSnapshotId": latest_id,
        "adminOverrideReason": request.reason,
    }
    if request.nightly_prices is not None:
        raw_payload["dailyPrices"] = {
            stay_date: str(amount)
            for stay_date, amount in request.nightly_prices.items()
        }
    else:
        raw_payload["originalTotal"] = str(request.total)
    try:
        snapshot = await record_source_price_snapshot(
            db,
            source_order_id=source.order_id,
            raw_payload=raw_payload,
            origin=origin,
            fetched_at=datetime.now(timezone.utc),
            created_by=current_user["user_id"],
            audit_log_id=audit.log_id,
        )
        audit.after_data = {
            "source_price_snapshot_id": snapshot.source_price_snapshot_id,
            "version": snapshot.version,
            "nightly_bases": snapshot.nightly_bases,
            "total": str(snapshot.total),
        }
        await db.commit()
        return snapshot
    except ValueError as exc:
        await db.rollback()
        raise HTTPException(
            422,
            detail={"code": "SOURCE_PRICE_OVERRIDE_INVALID", "message": str(exc)},
        ) from exc


@router.post(
    "/{order_id}/sync-conflicts/{conflict_id}/decision",
    response_model=SyncConflictDecisionOut,
)
async def decide_order_sync_conflict(
    order_id: str,
    conflict_id: str,
    body: SyncConflictDecisionRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    assert_can_write(current_user)
    if body.action == "ignore" and current_user["role"] != "admin":
        raise HTTPException(
            403,
            detail={
                "code": "SYNC_CONFLICT_IGNORE_FORBIDDEN",
                "message": "仅管理员可忽略宝寓差异",
            },
        )
    source = (
        await db.execute(
            select(Order)
            .where(Order.order_id == order_id, Order.is_deleted == False)
            .with_for_update()
        )
    ).scalar_one_or_none()
    if source is None:
        raise HTTPException(
            404,
            detail={"code": "ORDER_NOT_FOUND", "message": "订单不存在"},
        )
    conflict = (
        await db.execute(
            select(OrderSyncConflict)
            .where(
                OrderSyncConflict.conflict_id == conflict_id,
                OrderSyncConflict.source_order_id == order_id,
                OrderSyncConflict.status == OrderSyncConflictStatus.open,
            )
            .with_for_update()
        )
    ).scalar_one_or_none()
    if conflict is None:
        raise HTTPException(
            404,
            detail={"code": "SYNC_CONFLICT_NOT_FOUND", "message": "待处理差异不存在"},
        )
    if body.action == "preserve":
        _require_canonical_lock_writer()
        source.metadata_ = lock_fields(source.metadata_, {conflict.field})
    action = f"order.sync_conflict_{body.action}"
    audit = AuditLog(
        operator_id=current_user["user_id"],
        action=action,
        resource_type="order_sync_conflict",
        resource_id=conflict.conflict_id,
        before_data={
            "status": conflict.status.value,
            "local_value": conflict.local_value,
            "upstream_value": conflict.upstream_value,
        },
        after_data={
            "status": (
                OrderSyncConflictStatus.ignored.value
                if body.action == "ignore"
                else OrderSyncConflictStatus.open.value
            ),
            "decision": body.action,
        },
        notes=body.reason,
    )
    db.add(audit)
    await db.flush()
    if body.action == "ignore":
        conflict.status = OrderSyncConflictStatus.ignored
        conflict.ignored_by = current_user["user_id"]
        conflict.ignored_at = datetime.now(timezone.utc)
        conflict.ignored_audit_log_id = audit.log_id
    await db.commit()
    return {"conflict_id": conflict.conflict_id, "status": conflict.status.value}


@router.post("/{order_id}/zero-fee-split/preview", response_model=ZeroFeeSplitOut)
async def preview_zero_fee_split(
    order_id: str,
    body: dict,
    db: DBSession,
    current_user: CurrentUser,
):
    assert_can_write(current_user)
    _require_split_enabled()
    source = await db.get(Order, order_id)
    if source is None or source.is_deleted:
        raise HTTPException(404, detail={"code": "ORDER_NOT_FOUND", "message": "订单不存在"})
    try:
        request = _parse_zero_fee_split_body(body)
        return ZeroFeeSplitOut.model_validate(await preview_split(db, source, request))
    except (
        SplitStayError, SettledStayMutationError,
        IdempotencyKeyReusedError, OperationInProgressError,
    ) as exc:
        _raise_split_domain_http(exc)


@router.post("/{order_id}/zero-fee-split", response_model=ZeroFeeSplitOut)
async def execute_zero_fee_split(
    order_id: str,
    body: dict,
    db: DBSession,
    current_user: CurrentUser,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    assert_can_write(current_user)
    _require_split_enabled()
    if not idempotency_key or not idempotency_key.strip():
        raise HTTPException(
            422,
            detail={"code": "IDEMPOTENCY_KEY_REQUIRED", "message": "缺少 Idempotency-Key"},
        )
    source = await db.get(Order, order_id)
    if source is None or source.is_deleted:
        raise HTTPException(404, detail={"code": "ORDER_NOT_FOUND", "message": "订单不存在"})
    try:
        request = _parse_zero_fee_split_body(body)
        orders = await split_stay(
            db,
            source,
            request,
            current_user["user_id"],
            operation_key=idempotency_key.strip(),
        )
        group_id = orders[0].stay_group_id
        aggregate = await db.get(ManagedStayGroup, group_id)
        response = await _split_response(
            db, orders, version=aggregate.version, group_id=group_id
        )
        await db.commit()
        return response
    except (
        SplitStayError, SettledStayMutationError,
        IdempotencyKeyReusedError, OperationInProgressError,
    ) as exc:
        await db.rollback()
        _raise_split_domain_http(exc)
    except Exception:
        await db.rollback()
        raise


# ─── ID generator ────────────────────────────────────────────────────────────
# 实现收敛到 app.core.ids（与 booking/reconciliation 共享）；旧名保留给存量引用。
from app.core.ids import gen_order_id, gen_order_room_id, unique_order_id


# 房态联动/门闩/状态机已收敛到 services/order_state（#183）。
# 旧下划线别名保留：本文件内部与存量测试仍按旧名引用。
from app.services.order_state import (
    mark_room_reserved_if_available as _mark_room_reserved_if_available,
    release_reserved_room as _release_reserved_room,
    order_room_ids as _order_room_ids,
)

# ─── Create ──────────────────────────────────────────────────────────────────

@router.post("", response_model=OrderOut, status_code=201)
async def create_order(body: OrderCreate, db: DBSession, current_user: CurrentUser):
    assert_can_write(current_user)

    # body.rooms 由 OrderCreate.model_validator 保证非空（旧 payload 也合成单行）
    assert body.rooms, "OrderCreate.rooms should be populated by schema validator"

    # 每行房间分别冲突检测
    for i, r in enumerate(body.rooms):
        if r.room_id:
            has_conflict = await check_room_conflict(
                db, r.room_id, r.check_in_date, r.check_out_date
            )
            if has_conflict:
                raise HTTPException(
                    status_code=409,
                    detail=f"房间 {r.room_id} 在 {r.check_in_date}–{r.check_out_date} 已有订单冲突",
                )

    # 同客重复单拦截：上面的房间冲突检测只拦「同房间日期重叠」，未排房单
    # （room_id 为空）不设防——生产已出现同客同日期两笔单，未排房那笔常驻
    # 待排房池、已排房那笔画在甘特图上，运营误判为显示 bug。同名（去空格）
    # + 日期重叠（退房日排他，续住的连续单不算）+ 现存单非终态即拦；
    # 真有同名不同客时，前端确认后带 allow_duplicate=true 重发放行。
    if not body.allow_duplicate:
        guest_name = body.guest_name.strip()
        new_ci = min(r.check_in_date for r in body.rooms)
        new_co = max(r.check_out_date for r in body.rooms)
        dup = (await db.execute(
            select(Order)
            .where(
                Order.is_deleted == False,
                Order.order_status.not_in(TERMINAL_STATUSES),
                func.trim(Order.guest_name) == guest_name,
                Order.check_in_date < new_co,
                Order.check_out_date > new_ci,
            )
            .order_by(Order.created_at.asc())
            .limit(1)
        )).scalar_one_or_none()
        if dup:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"疑似重复订单：{guest_name} 已有未取消订单 {dup.order_id}"
                    f"（{dup.check_in_date} → {dup.check_out_date}，"
                    f"{order_status_label(dup.order_status)}），日期与本单重叠。"
                    "如确为同名不同客，请在确认后允许重复创建。"
                ),
                headers={"X-Error-Code": "duplicate_order"},
            )

    # 创建订单 + 子房行
    order_id = await unique_order_id(db)
    # ota_owner_revenue 不是 orders 列，是 metadata 字段，单独处理（见下方）。
    order_fields = body.model_dump(exclude={"rooms", "ota_owner_revenue", "auto_confirm", "allow_duplicate"})
    order = Order(
        order_id=order_id,
        created_by=current_user["user_id"],
        **order_fields,
    )
    # 手录平台单填了「到手价(净房费)」→ 写 metadata.ota_owner_revenue，佣金率自动倒推
    # 使 net_revenue≡到手价，公司毛收入以房费(actual_price)为准、不再低报。
    if body.ota_owner_revenue is not None:
        order.metadata_ = {**(order.metadata_ or {}),
                           "ota_owner_revenue": str(body.ota_owner_revenue)}
        sync_ota_commission_rate(order)
    for i, r in enumerate(body.rooms):
        order.rooms.append(OrderRoom(
            order_room_id=gen_order_room_id(),
            room_id=r.room_id,
            check_in_date=r.check_in_date,
            check_out_date=r.check_out_date,
            list_price=r.list_price,
            discount_amount=r.discount_amount,
            actual_price=r.actual_price,
            guests_count=r.guests_count,
            position=r.position if r.position is not None else i,
            daily_prices=compute_daily_prices(r.check_in_date, r.check_out_date, r.actual_price),
            # 每房净房费（手填才有）：str 存，与整单 metadata.ota_owner_revenue 同模式
            metadata_=(
                {"ota_owner_revenue": str(r.ota_owner_revenue)}
                if r.ota_owner_revenue is not None else {}
            ),
        ))
    db.add(order)

    # 房态联动：每行已分配房间触发 available → reserved
    for r in body.rooms:
        if r.room_id:
            await _mark_room_reserved_if_available(db, r.room_id)

    # Auto-create standard tasks
    await _create_standard_tasks(db, order, current_user["user_id"])

    # Auto-link guest record (find or create by phone)
    await link_guest_to_order(db, order)

    # 前台直达（auto_confirm）：创建 + 状态推进同一事务，守卫/房态联动复用
    # _apply_order_transition——任一步被守卫拦下即整单回滚，不留中间态。
    if body.auto_confirm:
        await db.flush()  # _apply_order_transition 内 _order_room_ids 直查 order_rooms 表
        # 全排房则由 _apply_order_transition 内部自动续推到「待入住」，不能再手工流转一次
        # （roomed_pending_checkin→roomed_pending_checkin 会被流转图拒绝）。
        await _apply_order_transition(db, order, OrderStatus.paid_pending_room)

    # 审计与建单同事务提交：order.rooms 已在 session 内 append，commit 前快照可用。
    await log_action_tx(db, current_user["user_id"], "order.create", "order", order_id,
                        after_data=order_snapshot(order))
    if body.auto_confirm:
        await log_action_tx(
            db, current_user["user_id"], "order.transition", "order", order_id,
            before_data={"_diff": {
                "status": OrderStatus.pending_confirm.value,
                "auto_confirm": True,
            }},
            after_data=order_snapshot(order),
        )

    await db.commit()
    # 预加载 rooms（OrderOut 序列化要用）
    refreshed = (await db.execute(
        select(Order)
        .where(Order.order_id == order_id)
        .options(selectinload(Order.rooms))
    )).scalar_one()

    return refreshed


async def _create_standard_tasks(db, order: Order, creator_id: str):
    # 订单创建时只建「收押金」task。清扫任务不在此处占位 —— 由 staff_portal.handle_checkout
    # 在客人退房时按所选保洁员建带 assignee 的 high-priority 任务，避免历史遗留的孤儿
    # cleaning task（生产上观察到 5 条无 assignee 的问题来源）。
    db.add(Task(
        task_id="TSK-" + uuid.uuid4().hex[:12].upper(),
        task_type=TaskType.collect_deposit,
        title=f"收取押金 — {order.guest_name}",
        order_id=order.order_id,
        room_id=order.room_id,
        priority=TaskPriority.high,
        created_by=creator_id,
    ))


# ─── List ─────────────────────────────────────────────────────────────────────

def _build_order_filters(
    *,
    status: Optional[str] = None,
    channel: Optional[Channel] = None,
    room_id: Optional[str] = None,
    check_in_from: Optional[date] = None,
    check_in_to: Optional[date] = None,
    check_out_from: Optional[date] = None,
    check_out_to: Optional[date] = None,
    keyword: Optional[str] = None,
    exclude_continuation_mid_checkout: bool = False,
    exclude_continuation_later_checkin: bool = False,
) -> list:
    """订单列表的筛选口径，`GET /orders` 与 `GET /orders/by-segment` 共用。

    单一来源：加筛选条件只改这里，两个端点自动一致。抽出来是因为两套筛选逻辑漂掉
    是必然事件，不是风险——本项目已经吃过「一份口径抄三份」的亏。
    """
    filters = [Order.is_deleted == False]
    if exclude_continuation_mid_checkout:
        # 「今日待退房」名单要与数字卡（dashboard._today_stats）同口径：续住组的
        # 中间段退房日=今天但客人不真走（去下一段续住），不算退房。判定「组末段」用
        # (check_out_date, order_id) 行值最大 —— 与 stay_group.group_last_order_id
        # 逐字一致（同日退房用 order_id 兜底，保证组内恰好一张末段）。
        # 剔除条件：存在同组活段(未删、未取消)其 (退房日, order_id) 严格更大 = 本单是中间段。
        # 无 stay_group_id 的单：子查询里 sib.group == order.group 遇 NULL 不匹配 →
        # NOT EXISTS 恒真 → 照常保留，无需额外分支。
        sib = aliased(Order)
        later_alive_sibling = (
            select(sib.order_id)
            .where(
                sib.stay_group_id == Order.stay_group_id,
                sib.is_deleted == False,
                sib.order_status != OrderStatus.cancelled,
                tuple_(sib.check_out_date, sib.order_id)
                > tuple_(Order.check_out_date, Order.order_id),
            )
        )
        managed_group = select(ManagedStayGroup.stay_group_id).where(
            ManagedStayGroup.stay_group_id == Order.stay_group_id
        )
        filters.append(or_(managed_group.exists(), ~later_alive_sibling.exists()))
    if exclude_continuation_later_checkin:
        # 上一条的镜像：「今日待入住」名单要与数字卡同口径。续住组的非首段入住日=今天，
        # 但客人昨天就住进来了，前台不用为它办入住。判定「组首段」用
        # (check_in_date, order_id) 行值最小 —— 与 stay_group.group_anchor 逐字一致
        # （同日入住用 order_id 兜底，保证组内恰好一张首段）。
        # 剔除条件：存在同组活段(未删、未取消)其 (入住日, order_id) 严格更小 = 本单非首段。
        # 无 stay_group_id 的单同样靠 NULL 不匹配 → NOT EXISTS 恒真 → 照常保留。
        sib_ci = aliased(Order)
        earlier_alive_sibling = (
            select(sib_ci.order_id)
            .where(
                sib_ci.stay_group_id == Order.stay_group_id,
                sib_ci.is_deleted == False,
                sib_ci.order_status != OrderStatus.cancelled,
                tuple_(sib_ci.check_in_date, sib_ci.order_id)
                < tuple_(Order.check_in_date, Order.order_id),
            )
        )
        managed_group_ci = select(ManagedStayGroup.stay_group_id).where(
            ManagedStayGroup.stay_group_id == Order.stay_group_id
        )
        filters.append(or_(managed_group_ci.exists(), ~earlier_alive_sibling.exists()))
    if status:
        # 支持逗号分隔多状态——Dashboard 今日清单卡要一次表达「待入住三态」/「待退房三态」，
        # 否则卡片计数(dashboard._today_stats)与点进来的列表对不上。单值行为不变。
        try:
            statuses = [OrderStatus(s.strip()) for s in status.split(",") if s.strip()]
        except ValueError:
            raise HTTPException(status_code=422, detail=f"无效的订单状态: {status}")
        if not statuses:
            raise HTTPException(status_code=422, detail="status 参数为空")
        filters.append(Order.order_status.in_(statuses))
    else:
        # 默认视图不显示已取消单——作废/OTA 取消单不该挂在列表碍事(自动取消只置 cancelled 不删)。
        # 想看已取消: 前端点「已取消」标签(status=cancelled)显式查询即可返回。
        filters.append(Order.order_status != OrderStatus.cancelled)
    if channel:
        filters.append(Order.channel == channel)
    if room_id:
        filters.append(Order.room_id == room_id)
    if check_in_from:
        filters.append(Order.check_in_date >= check_in_from)
    if check_in_to:
        filters.append(Order.check_in_date <= check_in_to)
    if check_out_from:
        filters.append(Order.check_out_date >= check_out_from)
    if check_out_to:
        filters.append(Order.check_out_date <= check_out_to)
    if keyword:
        filters.append(
            or_(
                Order.guest_name.ilike(f"%{keyword}%"),
                Order.guest_phone.ilike(f"%{keyword}%"),
                Order.order_id.ilike(f"%{keyword}%"),
            )
        )
    return filters


@router.get("", response_model=PaginatedOrderList)
async def list_orders(
    db: DBSession,
    current_user: CurrentUser,
    status: Optional[str] = Query(default=None),
    channel: Optional[Channel] = Query(default=None),
    room_id: Optional[str] = Query(default=None),
    check_in_from: Optional[date] = Query(default=None),
    check_in_to: Optional[date] = Query(default=None),
    check_out_from: Optional[date] = Query(default=None),
    check_out_to: Optional[date] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    exclude_continuation_mid_checkout: bool = Query(default=False),
    exclude_continuation_later_checkin: bool = Query(default=False),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    filters = _build_order_filters(
        status=status, channel=channel, room_id=room_id,
        check_in_from=check_in_from, check_in_to=check_in_to,
        check_out_from=check_out_from, check_out_to=check_out_to, keyword=keyword,
        exclude_continuation_mid_checkout=exclude_continuation_mid_checkout,
        exclude_continuation_later_checkin=exclude_continuation_later_checkin,
    )

    total = await db.scalar(select(func.count(Order.order_id)).where(*filters)) or 0

    q = (
        select(Order)
        .where(*filters)
        .options(selectinload(Order.rooms))  # multi-room: 填充 OrderListItem.room_ids
        .order_by(Order.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = (await db.execute(q)).scalars().all()

    return {"items": items, "total": total, "page": page, "page_size": page_size}


# 注意：必须在 /{order_id} 之前注册，否则 FastAPI 会按 path 参数把 "by-segment"
# 当成 order_id 路由到 get_order，返回一个看着像数据问题、其实是路由问题的 404。
@router.get("/by-segment", response_model=PaginatedSegmentList)
async def list_orders_by_segment(
    db: DBSession,
    current_user: CurrentUser,
    status: Optional[str] = Query(default=None),
    channel: Optional[Channel] = Query(default=None),
    room_id: Optional[str] = Query(default=None),
    check_in_from: Optional[date] = Query(default=None),
    check_in_to: Optional[date] = Query(default=None),
    check_out_from: Optional[date] = Query(default=None),
    check_out_to: Optional[date] = Query(default=None),
    keyword: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """订单列表，**按「段」分页**：一个续住组一行，一张无组单也一行。

    与 `GET /orders`（按单分页）并存，不是替代。两种语义天然不同：KPI 下钻抽屉
    要的是「给我 N 条单预览」，订单管理页要的是「给我 N 段」。硬塞进一个端点得加
    group_by 开关，那是坏味道。

    为什么不能按单分页：余慧那组（sg_4909d1907fb8）5 张单创建时间横跨 6/28→7/16，
    在 created_at 排序里隔着几十条，按单分页必然把它切到不同页，前端永远合不起来。

    每行的口径全部由 group_view 算，与详情页同源 —— 前端不做任何归并、不算任何合计。

    筛选语义：筛选决定哪些段出现（段内任一单匹配即出现），段一旦出现就整段返回、
    整段口径。
    """
    filters = _build_order_filters(
        status=status, channel=channel, room_id=room_id,
        check_in_from=check_in_from, check_in_to=check_in_to,
        check_out_from=check_out_from, check_out_to=check_out_to, keyword=keyword,
    )

    group_key = func.coalesce(Order.stay_group_id, Order.order_id)

    total = await db.scalar(
        select(func.count(func.distinct(group_key))).where(*filters)
    ) or 0

    # 第一步：取本页的段键（按段内最新 created_at 降序 —— 新续住单进来该段浮到前面）
    #
    # group_key 兜底排序不是装饰：created_at 是 server_default=func.now()，而 Postgres 的
    # now() 取事务开始时间 → ota-sync 一批导入的单 created_at 完全相同。并列时段序由数据库
    # 自由决定，而翻页是多次独立查询，两次之间的顺序可以不一样 → 一个段被两页都返回、或者
    # 一页都不返回。本地 SQLite 碰巧稳定，测不出来，别据此删掉它。
    page_keys = (await db.execute(
        select(group_key.label("gk"))
        .where(*filters)
        .group_by(group_key)
        .order_by(func.max(Order.created_at).desc(), group_key.asc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).all()
    keys = [row.gk for row in page_keys]

    if not keys:
        return PaginatedSegmentList(items=[], total=total, page=page, page_size=page_size)

    # 第二步：每个段键取一张代表单，交给 group_view 建段。
    #
    # 代表单**刻意不带 filters** —— 段一旦入选就整段口径。若这里带上 filters，
    # 筛 channel=ctrip 时代表单只会是携程那段，group_view 从它出发照样扫全组，
    # 结果没错但白绕；真正的坑是有人顺手把 filters 也传进 group_view，那就成了
    # 「列表 ¥877 / 详情 ¥3957」。别传。
    reps = (await db.execute(
        select(Order)
        .where(func.coalesce(Order.stay_group_id, Order.order_id).in_(keys),
               Order.is_deleted == False)
        .order_by(Order.check_in_date.asc(), Order.order_id.asc())
    )).scalars().all()

    rep_by_key: dict[str, Order] = {}
    for o in reps:                       # 已按入住日升序 → 首次见到的即该段最早那张
        rep_by_key.setdefault(o.stay_group_id or o.order_id, o)

    # 按第一步的 key 顺序出行（那才是排序结果；dict 顺序不是）
    items = [
        await _build_stay_group_out(db, rep_by_key[k])
        for k in keys if k in rep_by_key
    ]

    return PaginatedSegmentList(items=items, total=total, page=page, page_size=page_size)


# ─── Pending-room queue (operator running board) ─────────────────────────────
# 注意：必须在 /{order_id} 之前注册，否则 FastAPI 会按 path 参数把 "pending-room"
# 当成 order_id 路由到 get_order，返回 404。
@router.get("/pending-room", response_model=list[OrderOut])
async def list_pending_room_orders(db: DBSession, current_user: CurrentUser):
    """运营台「待排房订单池」专用：返回至少有一行待排房（OrderRoom.room_id IS NULL）
    且未取消/未完成的活跃订单，按入住日期升序，让操作员优先处理最近要入住的客人。
    多房订单只要有任一行未排房即出现在此列表。"""
    # pending_payment 已重定义为后期「待完成」态（2026-06-05），不再属于待排房池。
    active_statuses = [
        OrderStatus.pending_confirm,
        OrderStatus.paid_pending_room,
        OrderStatus.rescheduled,
    ]
    pending_subq = (
        select(OrderRoom.order_id)
        .where(OrderRoom.room_id.is_(None))
        .distinct()
    )
    q = (
        select(Order)
        .where(
            Order.is_deleted == False,
            Order.order_id.in_(pending_subq),
            Order.order_status.in_(active_statuses),
        )
        .options(selectinload(Order.rooms))  # OrderOut.rooms 序列化要用
        .order_by(Order.check_in_date.asc(), Order.created_at.asc())
    )
    return (await db.execute(q)).scalars().all()


# ─── Get detail ──────────────────────────────────────────────────────────────

@router.get("/{order_id}", response_model=OrderOut)
async def get_order(order_id: str, db: DBSession, current_user: CurrentUser):
    result = await db.execute(
        select(Order)
        .where(Order.order_id == order_id, Order.is_deleted == False)
        .options(selectinload(Order.rooms))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return order


# ─── Update ──────────────────────────────────────────────────────────────────

@router.patch("/{order_id}", response_model=OrderOut)
async def update_order(order_id: str, body: OrderUpdate, db: DBSession, current_user: CurrentUser,
                       background_tasks: BackgroundTasks):
    assert_can_write(current_user)

    result = await db.execute(
        select(Order)
        .where(Order.order_id == order_id, Order.is_deleted == False)
        .options(selectinload(Order.rooms))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    managed_forbidden_fields = {
        "channel", "booking_type", "rooms", "room_id", "check_in_date",
        "check_out_date", "list_price", "discount_amount", "actual_price",
        "platform_commission_rate", "ota_owner_revenue",
    }
    if body.model_fields_set & managed_forbidden_fields:
        await _reject_managed_ordinary_mutation(db, order)

    # 已完成订单也允许修改（2026-05-24 王总诉求：店长能补录/纠错），但要做两道防线：
    #   1) 改 actual_price 时不能改到 < 已收（否则账面变负）
    #   2) audit log 记 before/after 字段 diff，事后可追溯
    is_terminal_edit = order.order_status in TERMINAL_STATUSES
    if body.actual_price is not None and body.actual_price != order.actual_price:
        # 已收口径只算房费(排除押金),否则押金会抬高应收下限 (#48)
        total_paid = await sum_house_fee_paid(db, order_id)
        if body.actual_price < total_paid:
            raise HTTPException(
                status_code=400,
                detail=f"应收金额（¥{body.actual_price}）不能低于已收金额（¥{total_paid}）",
            )

    # 编辑单押金≤房费（批2 item3）：与建单 _cross_field_checks 同口径，仅 normal 单约束。
    # 按「合并态」判——改押金/房费、或把 booking_type 翻成 normal 任一触碰时都用合并后的
    # deposit/actual_price 校验，避免①先建单再 PATCH 抬押金 或②非 normal 单(押金可>房费)
    # 翻成 normal 绕过建单校验（评审 E）。
    _fields_set = body.model_fields_set
    _merged_bt = body.booking_type if body.booking_type is not None else order.booking_type
    _switching_to_normal = "booking_type" in _fields_set and body.booking_type == BookingType.normal
    if _merged_bt == BookingType.normal and (
        "deposit" in _fields_set or "actual_price" in _fields_set or _switching_to_normal
    ):
        _merged_deposit = body.deposit if "deposit" in _fields_set else order.deposit
        _merged_actual = body.actual_price if "actual_price" in _fields_set else order.actual_price
        if (
            _merged_deposit is not None and _merged_actual is not None
            and _merged_deposit > _merged_actual
        ):
            raise HTTPException(
                status_code=422,
                detail=f"押金（¥{_merged_deposit}）不能超过房费（¥{_merged_actual}）",
            )

    # 全字段快照（用于 audit diff + 操作日志卡片渲染）
    before_snapshot = order_snapshot(order)
    before = {"status": order.order_status.value}

    # 锁价判定取材：在 rooms 被替换/删除前，记下改动前的日期跨度与实收。
    # OTA 单(有 platform_order_id)若被运营续住/手动改价 → 价以运营为准，须打
    # metadata.price_locked，让 ota-sync 价格对账跳过、不覆盖回 bypms 权威价(landmine)。
    _lock_old_ci = min((r.check_in_date for r in order.rooms), default=None)
    _lock_old_co = max((r.check_out_date for r in order.rooms), default=None)
    _lock_old_actual = order.actual_price
    # 接手判定取材：改动前的渠道（平台→非平台的翻转要清冻结的平台定价残留，见下）。
    _old_channel = order.channel
    # 已入住换房换码判定取材：rooms 整体替换会生成全新 OrderRoom 行，改动前先按
    # position 记住每行房号，提交后逐 position 比对找出 old→new 换房对。
    _before_room_by_pos = {r.position: r.room_id for r in order.rooms}

    # ── 派生目标 rooms 列表 ──────────────────────────────────────────────────
    # 新前端：body.rooms 直接给（整体替换语义）
    # 老前端：传 body.room_id / body.check_in_date 等顶层字段 → 落到 order.rooms[0] 上
    # 都没传：保持 order.rooms 不动
    has_room_change = body.rooms is not None or any(
        getattr(body, f) is not None
        for f in ("room_id", "check_in_date", "check_out_date", "list_price")
    )
    target_rooms: Optional[list[OrderRoomCreate]] = None
    if body.rooms is not None:
        target_rooms = list(body.rooms)
    elif has_room_change and order.rooms:
        # 老前端单字段更新：以 order.rooms[0] 为基底叠加 body 顶层 diff
        base = order.rooms[0]
        target_rooms = [OrderRoomCreate(
            room_id=body.room_id if body.room_id is not None else base.room_id,
            check_in_date=body.check_in_date or base.check_in_date,
            check_out_date=body.check_out_date or base.check_out_date,
            list_price=body.list_price if body.list_price is not None else base.list_price,
            discount_amount=base.discount_amount,
            actual_price=base.actual_price,
            guests_count=base.guests_count,
            position=base.position,
            # 老客户端不认识每房净房费 → 原样带过重建，不能丢
            ota_owner_revenue=base.ota_owner_revenue,
        )]
        # 多余的房（order.rooms[1:]）保留 — 老 API 改不到它们
        target_rooms.extend([
            OrderRoomCreate(
                room_id=r.room_id,
                check_in_date=r.check_in_date,
                check_out_date=r.check_out_date,
                list_price=r.list_price,
                discount_amount=r.discount_amount,
                actual_price=r.actual_price,
                guests_count=r.guests_count,
                position=r.position,
                ota_owner_revenue=r.ota_owner_revenue,
            )
            for r in order.rooms[1:]
        ])

    # 接手翻转（平台→非平台）+ 老客户端形态：上面合成行「原样带过」的每房净值是
    # 平台时代旧值，须在建行前剥掉，否则结算按 房费−旧净值 扣不存在的佣金。
    # 显式 body.rooms 的净值是运营本次填的，保留。此处 order.channel 仍是改动前渠道
    #（顶层字段在后面才应用），订单级残留清理见下方接手块。
    if (
        body.channel is not None
        and order.channel in OTA_PLATFORM_CHANNELS
        and body.channel not in OTA_PLATFORM_CHANNELS
        and target_rooms is not None
        and body.rooms is None
    ):
        for _r in target_rooms:
            _r.ota_owner_revenue = None

    # 编辑禁选过去日期（批2 item5，评审 B/#3 修订）：只拦「把一张**当前未过期**的订单
    # 入住日推到今天之前」。已过期订单(入住日已 < 今天)允许改历史日期——那是店长补录/纠错
    # 老单的正当流程(CLAUDE.md 2026-05-24 王总诉求)，硬拦会误伤。判据取整单最早入住日：
    #   新最早入住日 < 今天  且  原最早入住日 未过期(>= 今天，或订单原本无房 None)。
    # 这样多房单里给任一房引入过去日期也会被拦(新最早必然 < 今天)，同时不挡历史单纠错。
    # allow_past_dates=true = 前端已弹窗让人确认过（王总 2026-07-17 拍板：纠错与手滑是同一个
    # 动作，机器分不出，拦死会挡正当纠错——改成默认拦、确认放行）。
    if target_rooms is not None and not body.allow_past_dates:
        _today = today_cn()
        _new_ci = min((r.check_in_date for r in target_rooms), default=None)
        _order_was_active = _lock_old_ci is None or _lock_old_ci >= _today
        if _new_ci is not None and _new_ci < _today and _order_was_active:
            raise HTTPException(
                status_code=400,
                detail=f"入住日期不能早于今天（{_today}）",
            )

    # ── 每房净房费一致性（写前判定，评审 2026-07-05 修复）──────────────────────
    # 整单净房费将被改写(body 带值)且与「将写入各房的每房净值」Σ 差 >1 分 → 清各房净值
    # 回退比例口径 + 留痕。必须在 rooms 重建**前**判/清：重建走 db.delete+db.add，
    # order.rooms 关系与新行失同步，重建后再读 order.rooms 清不到新行(identity-map 陷阱)。
    # 覆盖旧漏洞：legacy 顶层字段 PATCH 同时带整单净房费时旧版跳过守卫、静默算错。
    _per_room_net_cleared: Optional[dict] = None
    if body.ota_owner_revenue is not None:
        _src = target_rooms if target_rooms is not None else list(order.rooms)
        _snets = {
            (getattr(r, "order_room_id", None) or f"pos{i}"): r.ota_owner_revenue
            for i, r in enumerate(_src) if r.ota_owner_revenue is not None
        }
        if _snets and abs(sum(_snets.values()) - body.ota_owner_revenue) > Decimal("0.01"):
            _per_room_net_cleared = {k: str(v) for k, v in _snets.items()}
            if target_rooms is not None:
                for r in target_rooms:
                    r.ota_owner_revenue = None
            else:
                for r in order.rooms:
                    if (r.metadata_ or {}).get("ota_owner_revenue") is not None:
                        r.metadata_ = {k: v for k, v in r.metadata_.items()
                                       if k != "ota_owner_revenue"}

    # ── issue #103 Step 3：编辑路径也守住 normal 单的 list_price + 客人电话必填 ──
    # OrderCreate 在 schema 层校验，但 OrderUpdate 无对应 validator，否则可
    # 先正常下单再 PATCH 清空电话 / 整体替换 rooms 漏填 list_price 绕过规则。
    # 复用 schema 的 normal_booking_violation(单一真相源)，按"合并态"校验，且只在
    # 编辑实际触碰相关字段时拦——避免阻断仅改 notes/order_status 等无关字段的更新
    # (老数据可能本就缺这些字段)。
    fields_set = body.model_fields_set
    merged_booking_type = body.booking_type if body.booking_type is not None else order.booking_type
    switching_to_normal = "booking_type" in fields_set and body.booking_type == BookingType.normal
    # 电话：编辑触碰电话、或把单切成 normal 时按合并态校验
    check_phone = "guest_phone" in fields_set or switching_to_normal
    phone_after = body.guest_phone if "guest_phone" in fields_set else order.guest_phone
    # 客人价：替换 rooms 时校验新行；切到 normal 时校验现有行；否则不动
    rooms_to_check = target_rooms if target_rooms is not None else (order.rooms if switching_to_normal else None)
    violation = normal_booking_violation(
        merged_booking_type, phone_after, rooms_to_check,
        check_phone=check_phone, check_list_price=rooms_to_check is not None,
    )
    if violation:
        raise HTTPException(status_code=422, detail=violation)

    # ── 执行 rooms 替换（如果有）──────────────────────────────────────────
    if target_rooms is not None:
        old_rooms_snapshot = [(r.order_room_id, r.room_id) for r in order.rooms]
        old_room_ids = {rid for _, rid in old_rooms_snapshot if rid}
        new_room_ids = {r.room_id for r in target_rooms if r.room_id}

        # 手动换房锁（镜像 price_locked）：已排房的 OTA/bypms 单在我们系统里被改到别的房
        # （甘特拖拽换房走本 PATCH），打 room_locked 让 ota-sync 房号对账跳过、不倒回 bypms 原房，
        # 否则下一轮同步覆盖房号 → 与已发门锁码不符（进错房事故，landmine）。纯改期/首次排房不锁。
        if order.platform_order_id and old_room_ids and new_room_ids != old_room_ids:
            order.metadata_ = {**(order.metadata_ or {}), "room_locked": True}

        # 续住保留 daily_prices：按 position 抓旧 daily_prices，新 OrderRoom 创建时
        # 先尝试按日期复用旧值，新增日期默认 ¥0；若旧值之和 ≠ 新 actual_price 才回退
        # 到 compute_daily_prices 重新均摊（用户主动改了总价时走这条）。
        old_daily_by_pos: dict[int, dict] = {}
        for or_row in order.rooms:
            if or_row.daily_prices:
                old_daily_by_pos[or_row.position or 0] = dict(or_row.daily_prices)

        # 入住/退房章同样按 position 搬到新行去。rooms 是「整体替换」语义（下面把旧行 delete
        # 掉重建），而 checked_in_at / checked_out_at 是**操作事实**、不是订单属性——改个价、改个
        # 日期不该让系统忘了客人哪天住进来的、哪间已经退了。
        # 漏了它：改一次单 → 详情页「● 在住」却又冒出「办理入住（还剩 N 间）」（生产实例
        # ORD-20260716-7FC2，7/16 05:48 改单后章被擦）；单间退房认的也是 checked_out_at，
        # 擦掉后系统就忘了那间退过。#273 只堵了流转那条路，改单这条一直在造新的。
        old_stamps_by_pos: dict[int, tuple] = {}
        for or_row in order.rooms:
            old_stamps_by_pos[or_row.position or 0] = (or_row.checked_in_at, or_row.checked_out_at)

        # 1) 释放：旧列表里有但新列表里没的房间 → reserved 回 available
        for _, rid in old_rooms_snapshot:
            if rid and rid not in new_room_ids:
                await _release_reserved_room(db, rid)

        # 2) 删除旧 OrderRoom 行（必须用 ORM delete，否则 session 持有的 order.rooms
        #    collection 与 DB 不同步，cascade=all,delete-orphan 会把新 INSERT 撤回）
        for old_or in list(order.rooms):
            await db.delete(old_or)
        await db.flush()

        # 3) 冲突检测（每个新行，排除本订单）
        for r in target_rooms:
            if r.room_id:
                has_conflict = await check_room_conflict(
                    db, r.room_id, r.check_in_date, r.check_out_date,
                    exclude_order_id=order_id,
                )
                if has_conflict:
                    # 注意：此时旧行已删，事务回滚后会复原
                    raise HTTPException(
                        status_code=409,
                        detail=f"房间 {r.room_id} 在 {r.check_in_date}–{r.check_out_date} 已有订单冲突",
                    )

        # 4) 创建新 OrderRoom 行 + 房态联动（新出现的房间）
        from datetime import timedelta as _td
        for i, r in enumerate(target_rooms):
            pos = r.position if r.position is not None else i
            old_daily = old_daily_by_pos.get(pos, {})
            # 续住/缩短住语义：保留旧 daily_prices 按日期映射；新增日期 fallback 到上一日价
            # (与前端 editDatesMutation 同款行为，2026-05-28 王总确认续住默认按上一晚价
            # 自动填，避免运营点续住后再手动单日改价)。
            # 取价链必须与前端逐字一致：上一日价 → 旧均价。续住 ≥2 晚时第 2 个新增晚的
            # 「上一日」也是新增日、不在旧映射里，前端此时取旧均价(toFixed(2))；这里若给
            # 别的值，sum(preserved) ≠ 前端算好的 actual_price，会误走下方重新均摊路径、
            # 把周末/平日差价抹平。回归测试：tests/test_extend_stay_daily_prices.py。
            preserved: dict[str, str] = {}
            if old_daily:
                old_avg = (sum_daily_prices(old_daily) / len(old_daily)).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
                d = r.check_in_date
                while d < r.check_out_date:
                    k = d.isoformat()
                    if k in old_daily:
                        preserved[k] = str(old_daily[k])
                    else:
                        prev_k = (d - _td(days=1)).isoformat()
                        preserved[k] = str(old_daily.get(prev_k, old_avg))
                    d += _td(days=1)
            preserved_sum = sum_daily_prices(preserved) if preserved else None
            target_actual = Decimal(r.actual_price) if r.actual_price is not None else None
            # 判定走哪条路径：
            # - 有旧 daily_prices 且 sum(保留) == 新 actual_price → 保留续住语义
            # - 否则（新订单、用户主动改了 actual_price）→ 重新均摊
            if (
                preserved
                and target_actual is not None
                and preserved_sum is not None
                and abs(preserved_sum - target_actual) < Decimal("0.01")
            ):
                final_daily = preserved
            else:
                final_daily = compute_daily_prices(r.check_in_date, r.check_out_date, r.actual_price)
            _old_checked_in_at, _old_checked_out_at = old_stamps_by_pos.get(pos, (None, None))
            db.add(OrderRoom(
                order_room_id=gen_order_room_id(),
                order_id=order_id,
                room_id=r.room_id,
                check_in_date=r.check_in_date,
                check_out_date=r.check_out_date,
                list_price=r.list_price,
                discount_amount=r.discount_amount,
                actual_price=r.actual_price,
                guests_count=r.guests_count,
                position=pos,
                daily_prices=final_daily,
                # 操作事实照搬，不因改单丢失（见上方 old_stamps_by_pos）。新增的房行没有旧章 → None。
                checked_in_at=_old_checked_in_at,
                checked_out_at=_old_checked_out_at,
                # 整体替换语义：新行带净值则写、不带则自然清空（回退比例口径）
                metadata_=(
                    {"ota_owner_revenue": str(r.ota_owner_revenue)}
                    if r.ota_owner_revenue is not None else {}
                ),
            ))
            if r.room_id and r.room_id not in old_room_ids:
                await _mark_room_reserved_if_available(db, r.room_id)

        # 5) 同步顶层 deprecated 字段（首房 / min-max 跨度）— 老客户端读取兼容
        first = target_rooms[0]
        body_room_id_value = first.room_id
        body_checkin_value = min(r.check_in_date for r in target_rooms)
        body_checkout_value = max(r.check_out_date for r in target_rooms)
    else:
        body_room_id_value = None
        body_checkin_value = None
        body_checkout_value = None

    # ── 应用其它顶层字段更新 ────────────────────────────────────────────
    # 状态字段不允许经通用更新接口直接写,必须走 /transition 与 /deposit 端点,
    # 否则会绕过流转图/收齐校验/押金守卫/房态联动 (#40)。
    top_level_updates = body.model_dump(
        exclude_none=True,
        exclude={
            "rooms", "room_id", "check_in_date", "check_out_date", "list_price",
            "order_status", "payment_status", "deposit_status",
            "ota_owner_revenue",  # 非列，落 metadata（见下）
            "allow_past_dates",  # 非列，纯守卫开关（exclude_none 挡不住 bool False）
        },
    )
    for field, value in top_level_updates.items():
        setattr(order, field, value)

    # ── 平台单转线下接手：渠道从 OTA 平台改成非平台的瞬间，价格从此以运营为准 ──
    # 冻结的平台定价残留必须清：ota_owner_revenue 会让「净房费」一直显示旧平台到手价
    # （2026-08-02 张维伦单：房费 430 旁挂着 479.37）；ota_subsidy 残留会在结算端被计入
    # 业主收入（平台单已在 OTA 侧取消，补贴一分不会到账）。佣金率归零（线下无平台佣金）。
    # 有平台单号则打 price_locked：staging 里的原单价格口径已成僵尸，bypms 价格对账/
    # 补贴扫描不得再覆盖回来。触发条件必须是「本次翻转」——bypms 上门客单生来 channel=
    # offline 且带 ota_owner_revenue，那不是残留，不能按渠道现值误清。
    # 放在 body.ota_owner_revenue 写入之前：同一请求里显式补录的新净值仍然生效。
    if (
        body.channel is not None
        and _old_channel in OTA_PLATFORM_CHANNELS
        and order.channel not in OTA_PLATFORM_CHANNELS
    ):
        # 每房手填净值同为平台时代旧值：不清的话结算按 房费−旧净值 扣不存在的佣金。
        # rooms 未被本次替换 → 清存量行（必须先于下方 _meta 快照，helper 会写留痕）；
        # 老客户端合成行的净值已在合成处剥掉（见 target_rooms 合成块尾）。
        if target_rooms is None:
            clear_per_room_owner_revenue(order, reason="channel_adopted_offline")
        _meta = dict(order.metadata_ or {})
        _old_vals = {k: _meta.pop(k) for k in ("ota_owner_revenue", "ota_subsidy") if k in _meta}
        # 留痕**无条件**写（含空 old）：回退块靠它判断「锁是不是接手打的」。
        # 追加进 _log 列表（反复接手/回退不互相覆盖），最近一次镜像到单 key 便于快速读。
        _entry = {
            "old": {**_old_vals,
                    "platform_commission_rate": str(order.platform_commission_rate or 0)},
            "reason": "channel_adopted_offline",
            "from_channel": _old_channel.value,
            "to_channel": order.channel.value,
            "price_locked_before": bool(_meta.get("price_locked")),
            "at": datetime.now(timezone.utc).isoformat(),
            "by": current_user.get("user_id"),
        }
        _log = list(_meta.get("ota_pricing_cleared_log") or [])
        _log.append(_entry)
        _meta["ota_pricing_cleared_log"] = _log
        _meta["ota_pricing_cleared"] = _entry
        # 锁价不看有无平台单号：bypms 认领日后把 pid 挂回来时锁已就位，价格对账不会覆盖运营价。
        _meta["price_locked"] = True
        order.metadata_ = _meta
        order.platform_commission_rate = Decimal("0")
    elif (
        body.channel is not None
        and _old_channel not in OTA_PLATFORM_CHANNELS
        and order.channel in OTA_PLATFORM_CHANNELS
        and (order.metadata_ or {}).get("ota_pricing_cleared")
    ):
        # 误点接手的回退：渠道翻回平台 → 按留痕恢复冻结的平台定价与佣金率，
        # 撤掉接手时打的锁（接手前本就有锁的保留——那是改价挣来的）。佣金率随后
        # 由 sync_ota_commission_rate 按恢复的净值重推，与建单口径一致。
        _meta = dict(order.metadata_ or {})
        _entry = _meta.pop("ota_pricing_cleared")
        _old = _entry.get("old") or {}
        for k in ("ota_owner_revenue", "ota_subsidy"):
            if k in _old:
                _meta[k] = _old[k]
        if not _entry.get("price_locked_before"):
            _meta.pop("price_locked", None)
        _log = list(_meta.get("ota_pricing_cleared_log") or [])
        _log.append({
            "restored": _old,
            "reason": "channel_returned_platform",
            "from_channel": _old_channel.value,
            "to_channel": order.channel.value,
            "at": datetime.now(timezone.utc).isoformat(),
            "by": current_user.get("user_id"),
        })
        _meta["ota_pricing_cleared_log"] = _log
        order.metadata_ = _meta
        order.platform_commission_rate = safe_decimal(_old.get("platform_commission_rate"))

    # 补录/更正「到手价(净房费)」→ 写 metadata.ota_owner_revenue，下方 sync 倒推佣金率。
    if body.ota_owner_revenue is not None:
        # P3 退化守卫：补/更到手价时必须有房费，否则倒推不出佣金率、net=0 账面对不上。
        # 部分更新语义：只有本次不带 actual_price 且订单原本也无房费(顶层 + rooms 皆空)才拦，
        # 避免拦住"房费早已存在、只补到手价"的正常改单。actual_price 已由上方 top_level_updates
        # 循环并入 order.actual_price；替换 rooms 时用 target_rooms，否则用 order.rooms。
        rooms_for_price = target_rooms if target_rooms is not None else order.rooms
        if owner_revenue_requires_actual_violation(
            body.ota_owner_revenue, order.actual_price, rooms_for_price
        ):
            raise HTTPException(status_code=422, detail=OWNER_REVENUE_REQUIRES_ACTUAL)
        order.metadata_ = {**(order.metadata_ or {}),
                           "ota_owner_revenue": str(body.ota_owner_revenue)}

    # 强制同步 deprecated 顶层字段（避免和 order_rooms 漂移）
    if target_rooms is not None:
        order.room_id = body_room_id_value
        order.check_in_date = body_checkin_value
        order.check_out_date = body_checkout_value
        if target_rooms[0].list_price is not None:
            order.list_price = target_rooms[0].list_price

    # 每房净值清值留痕（写前已在 target_rooms/order.rooms 上剥离，见上方判定）
    if _per_room_net_cleared is not None:
        order.metadata_ = {**(order.metadata_ or {}),
            "per_room_owner_revenue_cleared": {
                "old": _per_room_net_cleared,
                "reason": "order_net_sum_mismatch",
                "new_order_total": str(body.ota_owner_revenue),
                "at": datetime.now(timezone.utc).isoformat(),
                "by": current_user.get("user_id"),
            }}

    # 根子统一：OTA 单 actual_price 变动后自动保持佣金率，使 net_revenue ≡ 携程到手
    # （手填单/占位单不动）。幂等，无条件调用。
    sync_ota_commission_rate(order)

    # 人工改价/续住锁价：OTA 单(有平台单号)的实收或日期被运营改动后，价格已偏离
    # bypms 权威口径 → 打 price_locked，使 ota-sync 价格对账(bypms_reconcile)跳过该单，
    # 不把运营定的价覆盖回去。纯换房(同日期同价)不锁，留待将来对账对齐。
    _dates_changed = target_rooms is not None and (
        min(r.check_in_date for r in target_rooms) != _lock_old_ci
        or max(r.check_out_date for r in target_rooms) != _lock_old_co
    )
    _price_changed = body.actual_price is not None and body.actual_price != _lock_old_actual
    if order.platform_order_id and (_dates_changed or _price_changed):
        order.metadata_ = {**(order.metadata_ or {}), "price_locked": True}

    # flush（而非 commit）后在同一事务内 expire+重查取 after 快照，审计随业务同事务提交。
    # 在 selectinload re-query 前 expire 全部 session 对象，避免 identity-map 命中
    # 已过期/被替换的 OrderRoom 行（多房 update 整体替换时容易踩中）
    await db.flush()
    db.expire_all()

    refreshed = (await db.execute(
        select(Order)
        .where(Order.order_id == order_id)
        .options(selectinload(Order.rooms))
    )).scalar_one()

    after_snapshot = order_snapshot(refreshed)
    apply_snapshot_manual_locks(
        refreshed, before_snapshot, after_snapshot, source="human"
    )
    await db.flush()
    await db.refresh(refreshed)
    after_snapshot = order_snapshot(refreshed)
    # diff 仅用于备注文本（"已完成订单修改：xxx, yyy"），全字段在 before/after_snapshot 里
    diff = {k: True for k in after_snapshot if before_snapshot.get(k) != after_snapshot.get(k)}
    action_name = "order.update_after_completed" if is_terminal_edit else "order.update"
    await log_action_tx(
        db, current_user["user_id"], action_name, "order", order_id,
        before_data=before_snapshot, after_data=after_snapshot,
        notes=("已完成订单修改：" + ", ".join(diff.keys())) if (is_terminal_edit and diff) else None,
    )
    await db.commit()

    # 智能门锁：退房日期变更则按各房新退房时间续期 active 客人码
    # （plan §16.1 决策5：挂 update 路径而非中转态）。失败不阻断更新。
    if str(after_snapshot.get("check_out_date")) != str(before_snapshot.get("check_out_date")):
        from app.services.lock.hooks import renew_codes_on_reschedule
        await renew_codes_on_reschedule(db, refreshed)

    # 智能门锁：已入住单经本 PATCH 换房（甘特拖拽落点走这里，不走 /transfer-room）→
    # 与换房端点同款换码：新房下码+定向撤旧房码+推飞书新卡。逐 position 比对房号，
    # 只改日期/价格不触发。后台异步 fail-safe，改单响应不等门锁。
    if refreshed.order_status == OrderStatus.checked_in:
        _after_room_by_pos = {r.position: r.room_id for r in refreshed.rooms}
        for _pos, _old_rid in _before_room_by_pos.items():
            _new_rid = _after_room_by_pos.get(_pos)
            if _old_rid and _new_rid and _new_rid != _old_rid:
                from app.services.lock.hooks import _process_transfer_relink
                background_tasks.add_task(_process_transfer_relink, order_id, _old_rid, _new_rid)

    return refreshed


@router.patch("/{order_id}/manual-overrides", response_model=OrderOut)
async def update_manual_overrides(
    order_id: str,
    body: ManualOverrideUpdate,
    db: DBSession,
    current_user: CurrentUser,
):
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可解除人工接管字段")
    _require_canonical_lock_writer()

    order = (await db.execute(
        select(Order)
        .where(Order.order_id == order_id, Order.is_deleted == False)
        .options(selectinload(Order.rooms))
        .with_for_update()
        .execution_options(populate_existing=True)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    if any(field.value in _MANAGED_STRUCTURAL_FIELDS for field in body.fields):
        await _reject_managed_ordinary_mutation(db, order)

    before_snapshot = order_snapshot(order)
    order.metadata_ = unlock_fields(order.metadata_, body.fields)
    await db.flush()
    await db.refresh(order)
    after_snapshot = order_snapshot(order)
    await log_action_tx(
        db,
        current_user["user_id"],
        "order.manual_override_unlock",
        "order",
        order_id,
        before_data=before_snapshot,
        after_data=after_snapshot,
        notes=body.reason,
    )
    await db.commit()
    return order


# ─── Single-day price update ─────────────────────────────────────────────────

@router.patch("/{order_id}/rooms/{order_room_id}/daily-price", response_model=OrderOut)
async def update_daily_price(
    order_id: str,
    order_room_id: str,
    body: DailyPriceUpdate,
    db: DBSession,
    current_user: CurrentUser,
):
    """修改某一晚的房价。重算该 OrderRoom.actual_price = sum(daily_prices)，
    再重算订单总 actual_price。"""
    assert_can_write(current_user)

    result = await db.execute(
        select(Order)
        .where(Order.order_id == order_id, Order.is_deleted == False)
        .options(selectinload(Order.rooms))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    await _reject_managed_ordinary_mutation(db, order)
    before_snapshot = order_snapshot(order)

    target_room = next((r for r in order.rooms if r.order_room_id == order_room_id), None)
    if not target_room:
        raise HTTPException(status_code=404, detail="房间行不存在")

    # 校验日期在入住区间 [check_in, check_out)
    if not (target_room.check_in_date <= body.date < target_room.check_out_date):
        raise HTTPException(
            status_code=400,
            detail=f"日期 {body.date} 不在房间入住区间 {target_room.check_in_date}–{target_room.check_out_date}",
        )

    date_key = body.date.isoformat()
    old_daily = dict(target_room.daily_prices or {})
    old_room_actual = target_room.actual_price
    old_order_actual = order.actual_price

    # 若 daily_prices 为空（老订单未回填），先用 actual_price 均摊一次再覆盖单日
    if not old_daily:
        old_daily = compute_daily_prices(
            target_room.check_in_date, target_room.check_out_date, target_room.actual_price
        )

    new_daily = dict(old_daily)
    new_daily[date_key] = str(body.price)

    # 续住均摊（王总 2026-05-26 视频确认的行为）：单日改价后，把新的 OrderRoom 总价
    # 平均到所有晚数，保证 chips 显示均价；用户改一天 = 调整总价 + 自动均摊。
    # 视频流程：原 2 晚 ¥339.44/晚 → 续住第 3 晚填 ¥400 → 自动均摊为 ¥359.63/晚。
    new_room_actual = sum_daily_prices(new_daily)
    new_daily = compute_daily_prices(
        target_room.check_in_date, target_room.check_out_date, new_room_actual
    )
    # 订单总 actual_price = 其它房 actual_price 之和 + 本房新 actual_price
    other_rooms_total = sum(
        (r.actual_price or Decimal("0")) for r in order.rooms if r.order_room_id != order_room_id
    )
    new_order_actual = other_rooms_total + new_room_actual

    # 已收金额校验：新总价不能低于已收房费（排除押金 #48，避免账面变负）
    total_paid = await sum_house_fee_paid(db, order_id)
    if new_order_actual < total_paid:
        raise HTTPException(
            status_code=400,
            detail=f"修改后订单总价（¥{new_order_actual}）不能低于已收金额（¥{total_paid}）",
        )

    # 应用变更（JSONB 必须重赋整个 dict，否则 SQLAlchemy 不会标记 dirty）
    target_room.daily_prices = new_daily
    target_room.actual_price = new_room_actual
    order.actual_price = new_order_actual
    # 改价使某房 actual_price 变动 → 手填的每房净房费（B 方案）不再对得上，若不清值
    # 结算会把整段涨/降价静默算成平台佣金。清各房净值回退比例口径 + 留痕（评审 2026-07-05）。
    clear_per_room_owner_revenue(
        order, reason="daily_price_edited",
        extra={"order_room_id": order_room_id, "by": current_user.get("user_id")})
    # 根子统一：单日改价后同样保持 OTA 单佣金率
    sync_ota_commission_rate(order)
    changed_sync_fields = apply_snapshot_manual_locks(
        order, before_snapshot, order_snapshot(order), source="human"
    )
    # 人工改价锁价：只有业务价格真的变化才锁。相同均价请求可能仅把旧订单空的
    # daily_prices 回填成均摊明细，不应因此阻断后续 bypms 同步。
    if order.platform_order_id and changed_sync_fields & {"actual_price", "daily_prices"}:
        order.metadata_ = {**(order.metadata_ or {}), "price_locked": True}

    await db.flush()
    db.expire_all()

    refreshed = (await db.execute(
        select(Order)
        .where(Order.order_id == order_id)
        .options(selectinload(Order.rooms))
    )).scalar_one()

    await log_action_tx(
        db, current_user["user_id"], "order.update_daily_price", "order", order_id,
        # before_data 保留单日改价的局部 diff（前端卡片在 _diff 区显示）；
        # after_data 是改价后的完整订单快照（卡片主体）
        before_data={
            **before_snapshot,
            "_diff": {
                "order_room_id": order_room_id,
                "date": date_key,
                "old_price": str(old_daily.get(date_key, "")),
                "old_room_actual": str(old_room_actual) if old_room_actual is not None else None,
                "old_order_actual": str(old_order_actual) if old_order_actual is not None else None,
            },
        },
        after_data=order_snapshot(refreshed),
        notes=f"修改单日房价 {date_key}：¥{old_daily.get(date_key, '0')} → ¥{body.price}",
    )
    await db.commit()
    return refreshed


# ─── Cancel ──────────────────────────────────────────────────────────────────

@router.post("/{order_id}/cancel")
async def cancel_order(order_id: str, db: DBSession, current_user: CurrentUser):
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="仅管理员或运营可直接取消订单")

    result = await db.execute(
        select(Order)
        .where(Order.order_id == order_id, Order.is_deleted == False)
        .options(selectinload(Order.rooms))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    if order.order_status in TERMINAL_STATUSES:
        raise HTTPException(status_code=400, detail="终态订单无法取消")
    # 已收押金不能直接取消，必须先调 /deposit/return 或 /deposit/withhold 把押金账走完。
    if order.deposit_status == DepositStatus.collected:
        raise HTTPException(
            status_code=400,
            detail="押金已收取，请先调用 /deposit/return 或 /deposit/withhold 处理后再取消订单",
        )

    before_snapshot = order_snapshot(order)
    order.order_status = OrderStatus.cancelled

    # 续住关联组：被取消的单立刻解关联，别让组挂着退订孤儿单（Task 12）。
    # 解关联前先抓组成员快照——撤码要按「取消前」的成员关系判断共享码是否仍被
    # 剩余活段覆盖，unlink 之后组号已清、查不回来。
    _prior_group_member_ids: list[str] = []
    if order.stay_group_id:
        from app.services import stay_group as _stay_group
        _prior_group_member_ids = [
            o.order_id for o in await _stay_group.get_group_orders(db, order.stay_group_id)
        ]
        try:
            await _stay_group.unlink_order(db, order)
        except SettledStayMutationError as exc:
            await db.rollback()
            _raise_split_domain_http(exc)

    # 房态联动：取消订单时遍历所有 OrderRoom，逐个释放 reserved 房间。
    # 已 occupied/cleaning 等是真实业务态，不动。
    for r in order.rooms:
        if r.room_id:
            await _release_reserved_room(db, r.room_id)

    apply_snapshot_manual_locks(
        order, before_snapshot, order_snapshot(order), source="human"
    )

    # order 已 selectinload rooms 且状态改动都在 session 内，commit 前直接快照并同事务写审计。
    await log_action_tx(
        db, current_user["user_id"], "order.cancel", "order", order_id,
        before_data=before_snapshot,
        after_data=order_snapshot(order),
    )
    await db.commit()

    # 智能门锁：取消订单作废客人码（plan §8.3）。失败不阻断取消。
    from app.services.lock.hooks import revoke_codes_on_cancel
    await revoke_codes_on_cancel(db, order, group_member_ids=_prior_group_member_ids or None)

    return {"message": "订单已取消"}


@router.delete("/{order_id}")
async def delete_order(order_id: str, db: DBSession, current_user: CurrentUser):
    """软删除订单。同时把状态推到 cancelled、释放 reserved 房间，
    保证 delete 与 cancel 的副作用一致（之前 delete 不释放房间，账面还会记入月报）。"""
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="仅管理员或运营可删除订单")

    result = await db.execute(
        select(Order)
        .where(Order.order_id == order_id, Order.is_deleted == False)
        .options(selectinload(Order.rooms))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    from app.services import stay_group as _managed_guard
    if await _managed_guard.is_managed_split(db, order.stay_group_id):
        _raise_split_domain_http(SettledStayMutationError(
            "managed_split 不能通过普通删除入口拆解；请使用住宿段更正流程"
        ))

    # 押金已收 → 强制先走 deposit 处理（同 cancel 行为）
    if order.deposit_status == DepositStatus.collected:
        raise HTTPException(
            status_code=400,
            detail="押金已收取，请先调用 /deposit/return 或 /deposit/withhold 处理后再删除订单",
        )

    before_snapshot = order_snapshot(order)
    order.is_deleted = True
    if order.order_status not in TERMINAL_STATUSES:
        order.order_status = OrderStatus.cancelled

    # 续住关联组：被删除的单立刻解关联（Task 12）。撤码需要「删除前」的组成员
    # 快照（同 cancel_order），先抓再解。
    _prior_group_member_ids: list[str] = []
    if order.stay_group_id:
        from app.services import stay_group as _stay_group
        _prior_group_member_ids = [
            o.order_id for o in await _stay_group.get_group_orders(db, order.stay_group_id)
        ]
        await _stay_group.unlink_order(db, order)

    # 释放房间（多房：遍历所有 OrderRoom 逐个释放 reserved）
    for r in order.rooms:
        if r.room_id:
            await _release_reserved_room(db, r.room_id)

    # 同步删除该订单下的任务
    tasks_result = await db.execute(select(Task).where(Task.order_id == order_id))
    for task in tasks_result.scalars().all():
        await db.delete(task)

    apply_snapshot_manual_locks(
        order, before_snapshot, order_snapshot(order), source="human"
    )

    # 审计与软删同事务提交：写失败则整单回滚，杜绝无痕删单
    #（「单据消失先查 order.delete」的排查惯例依赖此行必然存在）。
    # order 已 selectinload rooms 且改动都在 session 内，commit 前直接快照即可。
    await log_action_tx(
        db,
        current_user["user_id"],
        "order.delete",
        "order",
        order_id,
        before_data=before_snapshot,
        after_data=order_snapshot(order),
        notes="软删除订单 + 状态置 cancelled + 释放 reserved 房 + 清理关联任务",
    )
    await db.commit()

    # 撤门锁码(和 cancel 一致)——删单必须把码从锁上撤掉，否则 active 码留在物理锁上
    # 客人仍能开已删单的门；pending 码还会被 retry_pending 复活(landmine，2026-07-08 事故)。
    # fail-safe：门锁/厂商出问题不回滚已提交的删除。
    from app.services.lock.hooks import revoke_codes_on_cancel
    await revoke_codes_on_cancel(db, order, group_member_ids=_prior_group_member_ids or None)

    return {"message": "订单已删除"}


# ─── Status transition ────────────────────────────────────────────────────────

# 状态图与统一流转函数在 services/order_state（#183 收敛）。
# 单笔/批量 transition 一律经 apply_order_transition；cancel/delete 是**有意**比
# 状态图更宽的入口（任意非终态可取消/删除，含在住中），故各自持有押金守卫 +
# reserved 释放逻辑——改 apply_order_transition 的守卫/副作用时必须同步检查这两处。
from app.services.order_state import (
    apply_order_transition as _apply_order_transition,
)


# ─── Feature E: Batch operations ─────────────────────────────────────────────
# 注意:必须在 /{order_id}/transition 之前注册,否则 /batch/transition 会被
# 动态路由当成 order_id="batch" 吞掉(FastAPI 按声明顺序匹配)。

class BatchTransitionRequest(BaseModel):
    order_ids: list[str]
    target_status: str


@router.post("/batch/transition")
async def batch_transition(
    body: BatchTransitionRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """Batch transition multiple orders to a target status."""
    assert_can_write(current_user)

    try:
        target = OrderStatus(body.target_status)
    except ValueError:
        raise HTTPException(status_code=400, detail=f"无效的目标状态: {body.target_status}")

    succeeded = []
    failed = []

    from app.services import stay_group as stay_group_svc

    audit_diffs: list[tuple[str, str, dict]] = []
    for oid in body.order_ids:
        result = await db.execute(
            select(Order).where(Order.order_id == oid, Order.is_deleted == False)
            .options(selectinload(Order.rooms))
        )
        order = result.scalar_one_or_none()
        if not order:
            failed.append({"order_id": oid, "reason": "订单不存在"})
            continue
        before_snapshot = order_snapshot(order)

        # 续住组中间段闸：与单笔 /transition 同款——中间段不许单独退房/完成
        # （客人还没走，且完成的收尾/级联口径只认末段）。逐单失败，不整批炸。
        if target in (OrderStatus.pending_checkout, OrderStatus.completed) \
                and not await stay_group_svc.is_group_last_segment(db, order):
            failed.append({"order_id": oid,
                           "reason": "这是续住组的中间段，客人还没走。退房/完成请到组内最后一段办理。"})
            continue

        # 复用单笔流转的同一守卫+副作用,避免批量绕过押金/收齐校验与房态联动 (#40)
        try:
            before_status = await _apply_order_transition(db, order, target)
        except HTTPException as e:
            failed.append({"order_id": oid, "reason": e.detail})
            continue

        # 末段完成级联兄弟段：与单笔端点共用 complete_group_siblings，
        # 批量路径不许把前面几段留在 checked_in。
        if target == OrderStatus.completed:
            await stay_group_svc.complete_group_siblings(db, order)

        apply_snapshot_manual_locks(
            order, before_snapshot, order_snapshot(order), source="human"
        )
        audit_diffs.append((oid, before_status, before_snapshot))
        succeeded.append(oid)

    await db.flush()
    for oid, before_status, before_snapshot in audit_diffs:
        refreshed = (await db.execute(
            select(Order).where(Order.order_id == oid).options(selectinload(Order.rooms))
        )).scalar_one()
        await log_action_tx(
            db, current_user["user_id"], "order.transition", "order", oid,
            before_data={**before_snapshot, "_diff": {"status": before_status}},
            after_data=order_snapshot(refreshed),
        )
    await db.commit()
    return {"succeeded": succeeded, "failed": failed}


@router.post("/{order_id}/transition")
async def transition_status(
    order_id: str,
    target_status: OrderStatus,
    db: DBSession,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    # 与 staff_portal.handle_checkout 同款白名单:admin/operator/keeper 三角色才能改状态。
    # 保洁(cleaner)即使有 token 也不能调本 endpoint 完成订单(2026-05-28 王总反馈)。
    if current_user["role"] not in ("admin", "operator", "keeper"):
        raise HTTPException(status_code=403, detail="无权变更订单状态")
    result = await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
        .options(selectinload(Order.rooms))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    before_snapshot = order_snapshot(order)

    # 续住关联组：中间段（非末段）不许单独退房——客人还没走，退房请到组内最后一段办（Task 10）。
    if target_status == OrderStatus.pending_checkout:
        from app.services import stay_group as stay_group_svc
        if not await stay_group_svc.is_group_last_segment(db, order):
            raise HTTPException(status_code=400,
                detail="这是续住组的中间段，客人还没走。退房请到组内最后一段办理。")

    before_status = await _apply_order_transition(db, order, target_status)

    # 续住关联组：末段完成 → 级联兄弟段（与批量路径共用 complete_group_siblings）。
    if target_status == OrderStatus.completed and order.stay_group_id:
        from app.services import stay_group as stay_group_svc
        await stay_group_svc.complete_group_siblings(db, order)

    apply_snapshot_manual_locks(
        order, before_snapshot, order_snapshot(order), source="human"
    )

    await db.flush()
    refreshed = (await db.execute(
        select(Order).where(Order.order_id == order_id).options(selectinload(Order.rooms))
    )).scalar_one()
    await log_action_tx(
        db, current_user["user_id"], "order.transition", "order", order_id,
        before_data={**before_snapshot, "_diff": {"status": before_status}},
        after_data=order_snapshot(refreshed),
    )
    await db.commit()

    # 门锁联动(admin 网页/订单详情弹窗走 transition 办理入住的主路径——必须和
    # staff_portal.handle_checkin 一样下码,否则前台用弹窗办理入住时门锁静默不下码)。
    # 全部 fail-safe(hook 内部吞异常),门锁/厂商出问题绝不回滚已提交的状态流转。
    if target_status == OrderStatus.checked_in:
        # 后台化：下码(~3s 厂商往返)+飞书移到 BackgroundTask，响应先 flush→页面秒回，
        # 门锁码/卡片几秒后异步送达。用 FastAPI BackgroundTasks（响应后顺序执行、开独立
        # session），不用 asyncio.create_task（那是真并发，会和后续请求抢连接）。
        # 失败告警不静默（_process_checkin_codes 内部保证）。
        from app.services.lock.hooks import _process_checkin_codes
        background_tasks.add_task(_process_checkin_codes, order_id)
    elif target_status == OrderStatus.pending_checkout:
        from app.services.lock.hooks import revoke_codes_on_checkout
        await revoke_codes_on_checkout(db, refreshed)
    elif target_status == OrderStatus.cancelled:
        from app.services.lock.hooks import revoke_codes_on_cancel
        await revoke_codes_on_cancel(db, refreshed)

    if target_status == OrderStatus.completed:
        # 订单走完整流程 → 后台删掉押金小票 OSS 图，省空间（备注文本保留）。best-effort。
        from app.services.deposit_receipt_cleanup import cleanup_deposit_receipt_images
        background_tasks.add_task(cleanup_deposit_receipt_images, order_id)

    # 用 refreshed 而非请求的 target_status：确认订单可能已自动跳过「待排房」到「待入住」，
    # 返回真实落库状态，前端才能据此渲染下一步（否则会误显示「待排房/确认排房」）。
    return {"order_id": order_id, "new_status": refreshed.order_status.value}


# ─── 退房防呆：探测续住续单（只读提醒，不动门锁/订单）──────────────────────────────

@router.get("/{order_id}/checkout-precheck")
async def checkout_precheck(
    order_id: str,
    db: DBSession,
    current_user: CurrentUser,
):
    """退房前探测：本单是否有「续住续单」（同房同客、退房日紧接着的下一段订单）。

    有 → 前端退房弹窗提醒管家改用「门锁密码延期」沿用原密码，别办退房把码撤了
    （1613 事故 2026-07-09）。纯只读，不改任何门锁/订单；系统不自动判定续住，
    只把人从坑边拉回，是否退房仍由管家决定（王总 2026-07-08「不自动判定」原则）。
    """
    order = (await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    from app.services.checkout_continuation import find_checkout_continuations
    return {"continuations": await find_checkout_continuations(db, order)}


# ─── 门锁密码延期（续住手动沿用原密码，只动锁不动订单）────────────────────────────

class ExtendLockCodeRequest(BaseModel):
    new_checkout_date: date  # 续住的新退房日；把现有客人码延到这天 14:00


@router.post("/{order_id}/lock/extend-code")
async def extend_lock_code(
    order_id: str,
    body: ExtendLockCodeRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """续住手动延期：把该订单现有客人码的有效期延到新退房日，**密码不变**。
    只动门锁、不改订单日期/金额（续住来两个订单时避免合并订单动到按夜价格/对账）。
    前台自己确认是续住才点，系统不自动判定（规避重名/换客误配）。"""
    assert_can_write(current_user)
    order = (await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    # 用我们的编排层（orchestrator 的 OrderLockService，非 line 31 的换房桩）
    from app.services.lock.factory import get_lock_provider
    from app.services.lock.orchestrator import OrderLockService as _GuestLockService
    try:
        results = await _GuestLockService(get_lock_provider(), db).extend_guest_codes(
            order, body.new_checkout_date
        )
    except Exception:
        import logging
        logging.getLogger(__name__).exception("门锁延期失败 order=%s", order_id)
        raise HTTPException(status_code=502,
                            detail="门锁延期失败，请重试；仍不行可用慧享家 App 手动延期")
    if not results:
        # 区分「下发/重试中」与「真没码」：码 FAILED（锁一时离线）时没有 active 码可延，
        # 但 retry 轮会自动重推。别再吓前台说「未办理入住/失效」——等码下成后再点延期即可。
        from app.services.lock.hooks import has_inflight_guest_code
        if await has_inflight_guest_code(db, order):
            raise HTTPException(
                status_code=400,
                detail="门锁密码还在下发/重试中（锁可能一时离线），系统会自动重推；等它下成后再点延期")
        raise HTTPException(status_code=400,
                            detail="该订单没有可延期的有效门锁密码（未办理入住 / 房间未绑锁 / 码已失效）")

    # 豁免 log_action_tx（landmine）：本端点不改订单记录、自身无 db.commit()（门锁变更由
    # 锁服务处理），没有可加入的调用方事务；强行 tx 而无 commit 反而会丢审计。保留 fire-and-forget。
    await log_action(
        db, current_user["user_id"], "order.lock_extend", "order", order_id,
        after_data={"new_checkout_date": body.new_checkout_date.isoformat(), "results": results},
        notes="续住门锁密码延期（不改订单日期/金额）",
    )
    return {"results": results, "new_checkout_date": body.new_checkout_date.isoformat()}


@router.post("/{order_id}/lock/resend-card")
async def resend_lock_card(order_id: str, db: DBSession, current_user: CurrentUser):
    """把本单现有门锁密码卡重推到飞书密码群（2026-07-18）。

    场景：首发时飞书抖动/卡片被刷走/换房后想再要一张。只重推卡片、不碰门锁；
    没有可推的码（未办理入住 / 房间未绑锁 / 码已失效）→ 400 让前台知道没密码可发。
    """
    assert_can_write(current_user)
    order = (await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    from app.services.lock.hooks import notify_checkin_codes, has_inflight_guest_code
    # 押金上传卡入住时已发过，重发只发密码卡
    pushed = await notify_checkin_codes(db, order, include_deposit_card=False)
    if not pushed:
        # 区分「下发/重试中」与「真没码」：码 FAILED（锁一时离线）时 retry 轮会自动重推，
        # 别再吓前台说「未办理入住/失效」——那三条这时全是假的。
        if await has_inflight_guest_code(db, order):
            raise HTTPException(
                status_code=400,
                detail="门锁密码正在下发/重试中（锁可能一时离线），系统会自动重推，请稍候刷新，无需手动操作")
        raise HTTPException(
            status_code=400,
            detail="该订单没有可推送的门锁密码（未办理入住 / 房间未绑锁 / 码已失效）")

    # 豁免 log_action_tx：同 lock_extend——本端点不改订单记录、无调用方事务可加入。
    await log_action(
        db, current_user["user_id"], "order.lock_resend_card", "order", order_id,
        notes="重发门锁密码卡到飞书密码群",
    )
    return {"pushed": True}


@router.get("/{order_id}/lock/codes")
async def view_lock_codes(order_id: str, db: DBSession, current_user: CurrentUser):
    """查看本单当前门锁密码（解密后明文），供前台在详情里直接看+复制（2026-07-31）。

    兜底：飞书「重发密码卡」一抖就没退路、UI 从不显示密码本身 → 前台被卡在「想重新
    发密码都没地方发」。此端点把现有码解密回给前台（可见口径同 resend：active/pending
    的客人码）。门锁密码是敏感数据 → assert_can_write（仅 admin/operator，与 resend 同
    audience，不新增暴露面）。只读、不碰门锁；每次查看写审计。无可用码 → 空列表（不是
    400——读接口空是合法状态，前端据此显示「暂无可用密码」）。"""
    assert_can_write(current_user)
    order = (await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    from app.services.lock.hooks import gather_active_guest_codes, has_inflight_guest_code
    codes = await gather_active_guest_codes(db, order)

    # 空态区分「正在下发/重试中（会自愈）」与「真没码」：入住瞬间下码可能 FAILED（锁一时
    # 离线），gather 取不到但 retry 轮会自动重推 → issuing=True，前端据此显示诚实文案而非
    # 误导的「未办理入住/失效」，并轻量轮询自愈。有码时不必再判（codes 非空即 issuing=False）。
    issuing = bool(not codes and await has_inflight_guest_code(db, order))

    # 审计只在真取到密码时写（谁在何时看了哪单的密码）；空态不涉密、且前端会轮询自愈，
    # 若每次空查都写审计会刷屏。不记密码明文本身。
    if codes:
        await log_action(
            db, current_user["user_id"], "order.lock_code_view", "order", order_id,
            notes=f"查看门锁密码（{len(codes)} 间）",
        )
    return {"codes": codes, "issuing": issuing}


# ─── 续住关联（软关联：拴成一段连续入住，不合并不删单）────────────────────────────

@router.get("/{order_id}/link-candidates")
async def link_candidates(order_id: str, db: DBSession, current_user: CurrentUser):
    """本单可关联的续住续单（同房+日期相连，**不要求同名**）。只读。
    候选放宽到「已退房/已完成」老单，支持关联历史续住老单（做账用），不含已取消。
    require_same_name=False：夫妻用两个名字各定一张续住单也列为候选，前台肉眼核对后手动关联。"""
    from app.services.checkout_continuation import find_group_continuations, _LINKABLE_STATUSES
    order = (await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    # 已成组时按**房间**逐间从组内该房最晚退房日出发（find_group_continuations）：
    # 既支持第 N 段续接（#339 沈桑场景），也不会在一单多房时丢掉另一间房那条腿（程鹏 7/26）。
    return {"candidates": await find_group_continuations(
        db, order, statuses=_LINKABLE_STATUSES, require_same_name=False)}


class LinkContinuationRequest(BaseModel):
    next_order_id: str


@router.post("/{order_id}/link-continuation")
async def link_continuation_ep(order_id: str, body: LinkContinuationRequest,
                               db: DBSession, current_user: CurrentUser):
    from app.services import stay_group as stay_group_svc
    base = (await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )).scalar_one_or_none()
    nxt = (await db.execute(
        select(Order).where(Order.order_id == body.next_order_id, Order.is_deleted == False)
    )).scalar_one_or_none()
    if not base or not nxt:
        raise HTTPException(status_code=404, detail="订单不存在")
    before_by_id = {
        base.order_id: {"stay_group_id": base.stay_group_id},
        nxt.order_id: {"stay_group_id": nxt.stay_group_id},
    }
    # base 就是前台点开的那张单：组的解析在 link_continuation → find_group_continuations 里
    # 按房间做（gid = base.stay_group_id 仍继承原组号，第 N 段照样并进原组）。
    try:
        gid = await stay_group_svc.link_continuation(db, base, nxt)
    except SettledStayMutationError as e:
        _raise_split_domain_http(e)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    for changed_order in (base, nxt):
        apply_snapshot_manual_locks(
            changed_order,
            before_by_id[changed_order.order_id],
            {"stay_group_id": changed_order.stay_group_id},
            source="human",
        )
    await log_action_tx(
        db, current_user["user_id"], "order.link_continuation", "order", order_id,
        notes=f"关联续住 {base.order_id} ← {body.next_order_id}（组 {gid}；入口单 {order_id}）",
    )
    await db.commit()
    # 关联成功后：把锚单客人码延到组内最晚退房日（复用现成延期，密码不变）。
    # best-effort：门锁失败不回滚关联。**不即时告警**——锁只是短暂离线的话，第二道网
    # (extend_stale_group_codes 每 5 分钟) 会自动补延、锁一恢复即坐实；只有它连补 30 分钟
    # 仍失败（锁持续离线）才告警前台。这样躲过锁抖动的虚惊（王总 2026-07-21）。
    try:
        from app.services.lock.factory import get_lock_provider
        from app.services.lock.orchestrator import OrderLockService as _LockSvc
        anchor = await stay_group_svc.group_anchor(db, gid)
        final_co = await stay_group_svc.group_final_checkout_date(db, gid)
        if anchor and final_co:
            results = await _LockSvc(get_lock_provider(), db).extend_guest_codes(anchor, final_co)
            if results and any(not r.get("ok") for r in results):
                # 延期没当场坐实（锁离线）：只记日志，交第二道网重试+超时告警，前台不被立即打扰。
                logging.getLogger(__name__).info(
                    "续住关联延码未当场坐实 group=%s 锚单=%s→%s，交第二道网自动补延",
                    gid, anchor.order_id, final_co)
    except Exception:
        logging.getLogger(__name__).exception("续住关联门锁延期失败 group=%s", gid)
    return {"stay_group_id": gid}


@router.post("/{order_id}/unlink-continuation")
async def unlink_continuation_ep(order_id: str, db: DBSession, current_user: CurrentUser):
    from app.services import stay_group as stay_group_svc
    order = (await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    affected_orders = (
        await stay_group_svc.get_group_orders(db, order.stay_group_id)
        if order.stay_group_id else [order]
    )
    before_by_id = {
        affected.order_id: {"stay_group_id": affected.stay_group_id}
        for affected in affected_orders
    }
    try:
        await stay_group_svc.unlink_order(db, order)
    except SettledStayMutationError as exc:
        await db.rollback()
        _raise_split_domain_http(exc)
    for affected in affected_orders:
        apply_snapshot_manual_locks(
            affected,
            before_by_id[affected.order_id],
            {"stay_group_id": affected.stay_group_id},
            source="human",
        )
    await log_action_tx(
        db, current_user["user_id"], "order.unlink_continuation", "order", order_id,
        notes=f"取消续住关联 {order_id}",
    )
    await db.commit()
    return {"ok": True}


async def _build_stay_group_out(
    db, order: Order, *, with_candidates: bool = False,
) -> StayGroupOut:
    """把一张单所在的整段渲染成 StayGroupOut。详情端点与列表端点共用。

    单一来源：详情页与订单列表必须逐字段相同，共用这个函数是该性质的实现方式
    （test_by_segment_row_matches_detail_endpoint_exactly 钉死了它）。谁想给列表
    加「省 N+1」的捷径（比如直接 SUM(actual_price)），那条捷径不会排除已取消段，
    列表立刻开始和详情页各说各话 —— 要优化就优化 group_view 本身，别绕过它。

    with_candidates 只有详情端点用：列表每行再跑一次候选匹配是白烧查询，
    而且前台在列表上也点不了「关联」。
    """
    from app.services import stay_group as stay_group_svc

    view = await stay_group_svc.group_view(db, order)

    segs = (await db.execute(
        select(Order)
        .where(Order.order_id.in_(view["segment_order_ids"]))
        .options(selectinload(Order.rooms))   # OrderOut.rooms 序列化要用
    )).scalars().all()
    by_id = {s.order_id: s for s in segs}
    ordered = [by_id[oid] for oid in view["segment_order_ids"] if oid in by_id]

    # 候选按**房间**逐间从组内该房最晚退房日出发算（find_group_continuations，未成组则=本单）。
    # 已成组的组也要继续提示：客人再续一段时前台才能看到「看着像续住」并把第 N 段并进原组
    # （沈桑场景，王总 2026-07-24）。早先 `not order.stay_group_id` 把成组的组掐掉、导致合并
    # 一次后就再也提示不出来；按房间走还顺带修了「一单多房合并掉一条腿后够不着另一条腿」
    # （程鹏 1608+1609，2026-07-26）。
    #
    # 这里是**自动提示**（前台没点，系统主动弹「看着像续住」），口径必须保守：require_same_name
    # =True，只对同名客自动提示。否则从某间房往后找会命中该房的**下一位陌生客**，一点关联就
    # 把陌生人拴进本组（一个门锁密码发两拨人）。手动 /link-candidates 端点才放宽到 False——那是
    # 前台主动去找、带出对方姓名供肉眼核对，误配责任落在人点确认那步（夫妻各定一单靠它关联）。
    #
    # statuses 显式传 _LINKABLE_STATUSES：不传会默认落到 _UPCOMING_STATUSES，排除 completed/
    # pending_checkout，正好漏掉「关联历史老单做账」这个 _LINKABLE 专门为之存在的场景。
    #
    # 不包 try/except：字段对齐了就不该抛。硬吞异常会让候选静默为空还没人知道。
    candidates: list[LinkCandidateOut] = []
    if with_candidates:
        from app.services.checkout_continuation import (
            _LINKABLE_STATUSES, find_group_continuations,
        )
        for c in await find_group_continuations(
            db, order, statuses=_LINKABLE_STATUSES, require_same_name=True,
        ):
            candidates.append(LinkCandidateOut(**c))

    return StayGroupOut(
        stay_group_id=view["stay_group_id"],
        group_kind=view["group_kind"],
        anchor_order_id=view["anchor_order_id"],
        last_order_id=view["last_order_id"],
        check_in_date=view["check_in_date"],
        check_out_date=view["check_out_date"],
        nights=view["nights"],
        total_amount=view["total_amount"],
        group_status=view["group_status"],
        channels=view["channels"],
        rooms=view["rooms"],
        free_room_kind=view["free_room_kind"],
        deposit=view["deposit"],
        deposit_status=view["deposit_status"],
        deposit_order_id=view["deposit_order_id"],
        deposit_returned=view["deposit_returned"],
        segments=[OrderOut.model_validate(s) for s in ordered],
        segment_details=[StaySegmentDetail(**d) for d in view["segment_details"]],
        link_candidates=candidates,
    )


@router.get("/{order_id}/stay-group", response_model=StayGroupOut)
async def get_stay_group(order_id: str, db: DBSession, current_user: CurrentUser):
    """整段视图：点组内任意一段都返回同一份，供前端渲染统一详情页。

    只读，不改任何状态。非续住单返回单段退化组；无组但认出续住候选时把候选一并
    带回（触发条件归后端，前端不写「何时该问候选」的判断，也就不会跟关联校验的
    口径漂开）。
    """
    order = (await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    return await _build_stay_group_out(db, order, with_candidates=True)


# ─── Atomic room assignment (拖拽/选房用) ────────────────────────────────────

class AssignRoomRequest(BaseModel):
    room_id: str
    # Multi-room: 指定要排到哪一行 OrderRoom；不传则取第一个 room_id 为空的待排房行。
    # 若该订单只有一行（绝大多数老订单），不传即可。
    order_room_id: Optional[str] = None


@router.post("/{order_id}/assign-room", response_model=OrderOut)
async def assign_room(
    order_id: str,
    body: AssignRoomRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """运营台「拖拽 / 选房」专用：原子地完成排房 + 房态联动 + 自动状态推进。
    - 多房订单：通过 body.order_room_id 指定要排哪一行；省略时取第一个 room_id=NULL 的行
    - 校验 [check_in, check_out) 期间该房间无冲突订单（排除自身）
    - 旧房 reserved → available；新房 available → reserved
    - 若状态为 paid_pending_room **且全部 OrderRoom 都已排房** → 自动推到
      roomed_pending_checkin（多房订单要全部排完才推进）
    - 不允许给已入住/退房中/终态订单换房（与 OrderDetailModal 一致）
    """
    assert_can_write(current_user)

    result = await db.execute(
        select(Order)
        .where(Order.order_id == order_id, Order.is_deleted == False)
        .options(selectinload(Order.rooms))
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    await _reject_managed_ordinary_mutation(db, order)

    if order.order_status in (OrderStatus.checked_in, OrderStatus.pending_checkout, OrderStatus.completed, OrderStatus.cancelled):
        raise HTTPException(status_code=400, detail=f"当前状态「{order_status_label(order.order_status)}」不允许排房/换房")

    # 校验目标房间存在
    new_room = (await db.execute(select(Room).where(Room.room_id == body.room_id))).scalar_one_or_none()
    if not new_room:
        raise HTTPException(status_code=404, detail=f"房间 {body.room_id} 不存在")
    if new_room.room_status in (RoomStatus.maintenance, RoomStatus.locked):
        raise HTTPException(status_code=422, detail=f"房间 {body.room_id} 当前状态为 {new_room.room_status.value}，不可排房")

    # 定位要操作的 OrderRoom 行
    target_or: Optional[OrderRoom] = None
    if body.order_room_id:
        for r in order.rooms:
            if r.order_room_id == body.order_room_id:
                target_or = r
                break
        if target_or is None:
            raise HTTPException(status_code=404, detail=f"order_room_id {body.order_room_id} 不属于本订单")
    else:
        # 未指定 → 优先取第一个待排房行；都已排房则取首行（换房语义）
        target_or = next((r for r in order.rooms if r.room_id is None), None) or (order.rooms[0] if order.rooms else None)
        if target_or is None:
            raise HTTPException(status_code=400, detail="该订单无任何房间行，无法排房")

    # 冲突校验：排除本订单的该行（避免自冲突）
    has_conflict = await check_room_conflict(
        db, body.room_id, target_or.check_in_date, target_or.check_out_date,
        exclude_order_room_id=target_or.order_room_id,
    )
    if has_conflict:
        raise HTTPException(
            status_code=409,
            detail=f"房间 {body.room_id} 在 {target_or.check_in_date} → {target_or.check_out_date} 期间已有冲突订单",
        )

    before_snapshot = order_snapshot(order)
    old_room_id = target_or.room_id
    before = {"order_room_id": target_or.order_room_id, "room_id": old_room_id, "status": order.order_status.value}

    # 旧房回收
    if old_room_id and old_room_id != body.room_id:
        await _release_reserved_room(db, old_room_id)

    # 新房标记
    target_or.room_id = body.room_id
    await _mark_room_reserved_if_available(db, body.room_id)

    # 同步顶层 deprecated 字段（首房）
    if target_or.position == 0 or all(r.position != 0 for r in order.rooms if r is not target_or):
        order.room_id = body.room_id

    # 状态自动推进：仅当从「已支付待排房」开始 且 所有 OrderRoom 都已排房
    auto_transitioned = False
    all_assigned = all((r.room_id is not None) or (r is target_or) for r in order.rooms)
    if order.order_status == OrderStatus.paid_pending_room and all_assigned:
        order.order_status = OrderStatus.roomed_pending_checkin
        auto_transitioned = True

    apply_snapshot_manual_locks(
        order, before_snapshot, order_snapshot(order), source="human"
    )

    await db.flush()
    refreshed = (await db.execute(
        select(Order)
        .where(Order.order_id == order_id)
        .options(selectinload(Order.rooms))
    )).scalar_one()

    await log_action_tx(
        db, current_user["user_id"], "order.assign_room", "order", order_id,
        before_data={
            **before_snapshot,
            "_diff": {
                **before,
                "order_room_id": target_or.order_room_id,
                "new_room_id": body.room_id,
                "auto_transitioned": auto_transitioned,
            },
        },
        after_data=order_snapshot(refreshed),
    )
    await db.commit()
    return refreshed


# ─── 房间转移（换房）─────────────────────────────────────────────────────────

# 换房/对调允许状态集（单一真相源，前端 canTransferRoom 与此逐项对齐）。
# 覆盖所有「入住前可带房」态 + 入住中（走 midstay 按夜拆账）。
# 携程等平台单常以 pending_confirm 预排房落库，前台此时换房必须放行。
# 明确排除：pending_checkout / pending_payment（退房后态）、abnormal（在住异常，
# 不适用非 midstay 整段平移）、completed / cancelled（终态）。
TRANSFER_ALLOWED_STATUSES = {
    OrderStatus.pending_confirm,
    OrderStatus.paid_pending_room,
    OrderStatus.roomed_pending_checkin,
    OrderStatus.rescheduled,
    OrderStatus.checked_in,
}

# 对调（swap）额外允许「已完成」：两个已退房客人当初房号记错，需在甘特图上对调订正。
# 只换房号、不改价（markup=0），门锁/房态均为历史无副作用。单房换到空房不放开此状态。
SWAP_ALLOWED_STATUSES = TRANSFER_ALLOWED_STATUSES | {OrderStatus.completed}


async def recompute_payment_status(db, order_id: str, order: Order) -> None:
    """按已收非押金房费 vs 订单实收，重算 payment_status。换房加价后调用。"""
    total_paid = await sum_house_fee_paid(db, order_id)
    new_total = order.actual_price or Decimal("0")
    if total_paid <= 0:
        order.payment_status = PaymentStatus.unpaid
    elif total_paid < new_total:
        order.payment_status = PaymentStatus.partial
    else:
        order.payment_status = PaymentStatus.paid


@router.post("/{order_id}/transfer-room", response_model=OrderOut)
async def transfer_room(
    order_id: str, body: TransferRoomRequest, db: DBSession, current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """换房：覆盖免费升级/房间故障/客人要求。入住中按换房日拆分 OrderRoom，
    加价计入新房行（业主正常分成），故障旧房转维修+建维修任务，门锁联动走钩子。"""
    assert_can_write(current_user)

    order = (await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
        .options(selectinload(Order.rooms))
    )).scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")
    await _reject_managed_ordinary_mutation(db, order)

    if order.order_status not in TRANSFER_ALLOWED_STATUSES:
        raise HTTPException(status_code=400,
            detail=f"当前状态「{order_status_label(order.order_status)}」不允许换房")

    target = next((r for r in order.rooms if r.order_room_id == body.order_room_id), None)
    if target is None:
        raise HTTPException(status_code=404, detail=f"order_room_id {body.order_room_id} 不属于本订单")
    old_room_id = target.room_id
    if body.new_room_id == old_room_id:
        raise HTTPException(status_code=400, detail="新房间与原房间相同")

    new_room = await db.get(Room, body.new_room_id)
    if not new_room:
        raise HTTPException(status_code=404, detail=f"房间 {body.new_room_id} 不存在")
    if new_room.room_status in (RoomStatus.maintenance, RoomStatus.locked):
        raise HTTPException(status_code=422,
            detail=f"房间 {body.new_room_id} 当前为 {new_room.room_status.value}，不可换入")

    midstay = order.order_status == OrderStatus.checked_in
    eff_date = body.transfer_date or (today_cn() if midstay else target.check_in_date)
    if not (target.check_in_date <= eff_date < target.check_out_date):
        raise HTTPException(status_code=422,
            detail=f"换房日 {eff_date} 不在入住区间 {target.check_in_date}–{target.check_out_date}")

    # 冲突校验：新房在 [conflict_start, 退房) 段无冲突。order_rooms 无 GIST 防重叠约束（T3），
    # 并发安全靠 check_room_conflict 内对 Room 行的 SELECT FOR UPDATE 串行化（与 assign-room 同款）。
    conflict_start = eff_date if midstay else target.check_in_date
    if await check_room_conflict(db, body.new_room_id, conflict_start, target.check_out_date,
                                 exclude_order_room_id=target.order_room_id):
        raise HTTPException(status_code=409,
            detail=f"房间 {body.new_room_id} 在 {conflict_start} → {target.check_out_date} 已有冲突订单")

    order_before_snapshot = order_snapshot(order)
    before_diff = {"order_room_id": body.order_room_id, "old_room_id": old_room_id,
                   "new_room_id": body.new_room_id, "reason": body.reason.value,
                   "markup": str(body.markup_amount), "transfer_date": eff_date.isoformat()}

    await apply_room_transfer(
        db, order, order_room_id=body.order_room_id, new_room_id=body.new_room_id,
        reason=body.reason, transfer_date=eff_date,
        markup_amount=body.markup_amount, old_room_disposition=body.old_room_disposition,
    )

    # 加价后重算 payment_status（加价 = 待收）。加价只增不减 → 不会"总价低于已收"。
    await recompute_payment_status(db, order_id, order)
    apply_snapshot_manual_locks(
        order, order_before_snapshot, order_snapshot(order), source="human"
    )

    # 故障 → 建维修任务（用 custom 类型，不加 PG enum）
    if body.reason == TransferReason.room_defect and old_room_id:
        old_room = await db.get(Room, old_room_id)
        db.add(Task(
            task_id=f"T{uuid.uuid4().hex[:18]}", task_type=TaskType.custom,
            title=f"房间维修-{old_room.room_name if old_room else old_room_id}",
            description=f"订单 {order_id} 因房间故障换房，{old_room_id} 转维修待修复",
            order_id=order_id, room_id=old_room_id,
            priority=TaskPriority.high, status=TaskStatus.pending,
            created_by=current_user["user_id"],
        ))

    await db.flush()

    refreshed = (await db.execute(
        select(Order).where(Order.order_id == order_id).options(selectinload(Order.rooms))
    )).scalar_one()

    await log_action_tx(db, current_user["user_id"], "order.transfer_room", "order", order_id,
                        before_data={**order_before_snapshot, "_diff": before_diff},
                        after_data=order_snapshot(refreshed))
    await db.commit()

    # 门锁联动：仅已入住换房需换码（新房下码+撤旧房码+推飞书卡）。后台异步、fail-safe，
    # 换房响应不等门锁。未入住换房无码不动。
    if midstay:
        from app.services.lock.hooks import _process_transfer_relink
        background_tasks.add_task(_process_transfer_relink, order_id, old_room_id, body.new_room_id)

    return refreshed


async def _set_room_static(db, room_id: str | None, status: RoomStatus) -> None:
    """对调后显式校正房间静态房态。两次 apply_room_transfer 的 _dispose_old_room
    会把刚被对方占用的房误标为空，故末尾按最终占用方重新置 occupied/reserved。
    previous_status 记 available（与 _dispose_old_room 约定一致，便于一键恢复）。"""
    if not room_id:
        return
    room = await db.get(Room, room_id)
    if room and room.room_status != status:
        room.previous_status = RoomStatus.available
        room.room_status = status


@router.post("/swap-rooms", response_model=list[OrderOut])
async def swap_rooms(body: SwapRoomsRequest, db: DBSession, current_user: CurrentUser,
                     background_tasks: BackgroundTasks):
    """对调两个订单的房间号：各保留自己日期、不改价（markup=0）。单事务原子，
    先双向冲突校验再复用 apply_room_transfer。入住↔入住禁止。"""
    assert_can_write(current_user)
    if body.order_a_id == body.order_b_id:
        raise HTTPException(status_code=400, detail="不能与自己对调")

    async def _load(oid: str) -> Order | None:
        return (await db.execute(
            select(Order).where(Order.order_id == oid, Order.is_deleted == False)
            .options(selectinload(Order.rooms))
        )).scalar_one_or_none()

    order_a = await _load(body.order_a_id)
    order_b = await _load(body.order_b_id)
    if not order_a or not order_b:
        raise HTTPException(status_code=404, detail="订单不存在")
    await _reject_managed_ordinary_mutation(db, order_a, order_b)

    for o in (order_a, order_b):
        if o.order_status not in SWAP_ALLOWED_STATUSES:
            raise HTTPException(status_code=400,
                detail=f"当前状态「{order_status_label(o.order_status)}」不允许对调")
    midstay_a = order_a.order_status == OrderStatus.checked_in
    midstay_b = order_b.order_status == OrderStatus.checked_in
    if midstay_a and midstay_b:
        raise HTTPException(status_code=400, detail="两个订单都在住，不支持对调")

    row_a = next((r for r in order_a.rooms if r.order_room_id == body.order_room_a_id), None)
    row_b = next((r for r in order_b.rooms if r.order_room_id == body.order_room_b_id), None)
    if row_a is None or row_b is None:
        raise HTTPException(status_code=404, detail="order_room 不属于对应订单")
    room_a, room_b = row_a.room_id, row_b.room_id
    if not room_a or not room_b:
        raise HTTPException(status_code=400, detail="订单尚未排房，无法对调")
    if room_a == room_b:
        raise HTTPException(status_code=400, detail="两个订单已在同一房间，无需对调")

    room_obj_a = await db.get(Room, room_a)
    room_obj_b = await db.get(Room, room_b)
    if not room_obj_a or not room_obj_b:
        raise HTTPException(status_code=404, detail="对调涉及的房间不存在")
    for room_obj, label in [(room_obj_b, room_b), (room_obj_a, room_a)]:
        if room_obj.room_status in (RoomStatus.maintenance, RoomStatus.locked):
            raise HTTPException(status_code=422,
                detail=f"房间 {label} 当前为 {room_obj.room_status.value}，不可换入")

    a_start = today_cn() if midstay_a else row_a.check_in_date
    b_start = today_cn() if midstay_b else row_b.check_in_date
    if midstay_a and not (row_a.check_in_date <= a_start < row_a.check_out_date):
        raise HTTPException(status_code=422, detail="入住订单 A 的对调日不在入住区间内")
    if midstay_b and not (row_b.check_in_date <= b_start < row_b.check_out_date):
        raise HTTPException(status_code=422, detail="入住订单 B 的对调日不在入住区间内")

    if await check_room_conflict(db, room_b, a_start, row_a.check_out_date,
                                 exclude_order_room_id=row_b.order_room_id):
        raise HTTPException(status_code=409,
            detail=f"房间 {room_b} 在 {a_start} → {row_a.check_out_date} 已被其它订单占用，无法对调")
    if await check_room_conflict(db, room_a, b_start, row_b.check_out_date,
                                 exclude_order_room_id=row_a.order_room_id):
        raise HTTPException(status_code=409,
            detail=f"房间 {room_a} 在 {b_start} → {row_b.check_out_date} 已被其它订单占用，无法对调")

    before_a = order_snapshot(order_a)
    before_b = order_snapshot(order_b)
    snap = {"a": {"order_id": order_a.order_id, "old_room": room_a, "new_room": room_b},
            "b": {"order_id": order_b.order_id, "old_room": room_b, "new_room": room_a}}

    await apply_room_transfer(db, order_a, order_room_id=row_a.order_room_id, new_room_id=room_b,
        reason=TransferReason.swap, transfer_date=a_start,
        markup_amount=Decimal("0"), old_room_disposition=None)
    await apply_room_transfer(db, order_b, order_room_id=row_b.order_room_id, new_room_id=room_a,
        reason=TransferReason.swap, transfer_date=b_start,
        markup_amount=Decimal("0"), old_room_disposition=None)

    await _set_room_static(db, room_b, RoomStatus.occupied if midstay_a else RoomStatus.reserved)
    await _set_room_static(db, room_a, RoomStatus.occupied if midstay_b else RoomStatus.reserved)

    await recompute_payment_status(db, order_a.order_id, order_a)
    await recompute_payment_status(db, order_b.order_id, order_b)
    apply_snapshot_manual_locks(order_a, before_a, order_snapshot(order_a), source="human")
    apply_snapshot_manual_locks(order_b, before_b, order_snapshot(order_b), source="human")

    await db.flush()

    async def _refresh(oid: str) -> Order:
        return (await db.execute(
            select(Order).where(Order.order_id == oid).options(selectinload(Order.rooms))
        )).scalar_one()

    refreshed_a = await _refresh(order_a.order_id)
    refreshed_b = await _refresh(order_b.order_id)

    await log_action_tx(db, current_user["user_id"], "order.swap_room", "order", order_a.order_id,
                        before_data={**before_a, "_diff": snap["a"]},
                        after_data=order_snapshot(refreshed_a))
    await log_action_tx(db, current_user["user_id"], "order.swap_room", "order", order_b.order_id,
                        before_data={**before_b, "_diff": snap["b"]},
                        after_data=order_snapshot(refreshed_b))
    await db.commit()

    # 对调门锁联动：只有在住那一方需换码（两单都在住已被 400 拦）。后台 fail-safe。
    from app.services.lock.hooks import _process_transfer_relink
    for oid, old_r, new_r, is_mid in [
        (order_a.order_id, room_a, room_b, midstay_a),
        (order_b.order_id, room_b, room_a, midstay_b),
    ]:
        if is_mid:
            background_tasks.add_task(_process_transfer_relink, oid, old_r, new_r)

    return [refreshed_a, refreshed_b]


# ─── Feature 9: Deposit workflow ─────────────────────────────────────────────

class DepositWithholdRequest(BaseModel):
    reason: str


class DepositReturnRequest(BaseModel):
    # 实退金额。不传 = 全退（= order.deposit）。可少于 deposit：差额即扣款。
    refund_amount: Optional[Decimal] = None
    # 当实退 < 押金（有扣款）时必填扣款原因（客人损坏房间物品等）。
    withhold_reason: Optional[str] = None


@router.post("/{order_id}/deposit/collect")
async def collect_deposit(order_id: str, db: DBSession, current_user: CurrentUser):
    """标记押金为已收取"""
    assert_can_write(current_user)

    result = await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    if order.deposit_status != DepositStatus.not_collected:
        raise HTTPException(status_code=400, detail=f"当前押金状态为 {order.deposit_status.value}，无法收取")

    # 续住关联组：押金一趟只收一次，收在 deposit_holder 认定的那段（全未收时是
    # 「deposit>0 的最早活段」，都为 0 才是锚单）。别的段上点收 → 400 指清去哪段。
    if order.stay_group_id:
        from app.services import stay_group as stay_group_svc
        holder = await stay_group_svc.deposit_holder(db, order)
        if holder.order_id != order.order_id:
            raise HTTPException(status_code=400,
                detail=f"续住组押金应收在 {holder.order_id} 段，请到该段收取，本段无需重复收押金。")

    order.deposit_status = DepositStatus.collected
    await db.flush()
    refreshed = (await db.execute(
        select(Order).where(Order.order_id == order_id).options(selectinload(Order.rooms))
    )).scalar_one()
    await log_action_tx(
        db, current_user["user_id"], "deposit.collect", "order", order_id,
        after_data=order_snapshot(refreshed),
    )
    await db.commit()
    return {"message": "押金已标记为已收取", "deposit_status": DepositStatus.collected.value}


@router.post("/{order_id}/deposit/return")
async def return_deposit(
    order_id: str,
    db: DBSession,
    current_user: CurrentUser,
    body: DepositReturnRequest | None = None,
):
    """退还押金并记录实退金额（2026-06-05 王总补充）。

    实退金额默认全退（= order.deposit），可改少；差额即扣款（客人损坏房间物品），
    此时必填扣款原因。实退额写入 order.deposit_returned，扣款原因写入
    metadata_['deposit_withhold_reason']。
      - 实退 == 押金        → deposit_status = returned
      - 0 < 实退 < 押金      → deposit_status = returned（部分退，差额为扣款，需原因）
      - 实退 == 0           → deposit_status = withheld（全扣，需原因）
    """
    assert_can_write(current_user)
    body = body or DepositReturnRequest()

    result = await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    # 续住关联组：退押金认 deposit_holder（真押着钱的那段），与 collect 对称——
    # 在别的段上点退 → 400 指清持有段，别让前台以为「没收过」而漏退现金。
    if order.stay_group_id:
        from app.services import stay_group as stay_group_svc
        _holder = await stay_group_svc.deposit_holder(db, order)
        if _holder.order_id != order.order_id:
            raise HTTPException(status_code=400,
                detail=f"续住组押金挂在 {_holder.order_id} 段，请到该段办理退还。")

    if order.deposit_status != DepositStatus.collected:
        raise HTTPException(status_code=400, detail=f"当前押金状态为 {order.deposit_status.value}，需先收取后才能退还")

    deposit_total = order.deposit or Decimal("0")
    refund = body.refund_amount if body.refund_amount is not None else deposit_total
    if refund < 0 or refund > deposit_total:
        raise HTTPException(status_code=400, detail=f"实退金额必须在 0 ~ {deposit_total:.2f} 之间")

    withhold = deposit_total - refund
    if withhold > 0 and not (body.withhold_reason and body.withhold_reason.strip()):
        raise HTTPException(status_code=400, detail="实退少于押金时必须填写扣款原因")

    order.deposit_returned = refund
    order.deposit_status = DepositStatus.withheld if refund == 0 else DepositStatus.returned
    if withhold > 0:
        meta = dict(order.metadata_) if order.metadata_ else {}
        meta["deposit_withhold_reason"] = body.withhold_reason.strip()
        order.metadata_ = meta

    await db.flush()
    refreshed = (await db.execute(
        select(Order).where(Order.order_id == order_id).options(selectinload(Order.rooms))
    )).scalar_one()
    snap = order_snapshot(refreshed)
    snap["_diff"] = {
        "refund_amount": str(refund),
        "withhold_amount": str(withhold),
        "withhold_reason": body.withhold_reason if withhold > 0 else None,
    }
    await log_action_tx(
        db, current_user["user_id"], "deposit.return", "order", order_id,
        after_data=snap,
    )
    await db.commit()
    return {
        "message": "押金已退还" if refund == deposit_total else f"押金已退还 ¥{refund:.2f}，扣款 ¥{withhold:.2f}",
        "deposit_status": order.deposit_status.value,
        "deposit_returned": str(refund),
    }


@router.post("/{order_id}/deposit/withhold")
async def withhold_deposit(
    order_id: str,
    body: DepositWithholdRequest,
    db: DBSession,
    current_user: CurrentUser,
):
    """标记押金为扣留（附扣留原因）"""
    assert_can_write(current_user)

    result = await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )
    order = result.scalar_one_or_none()
    if not order:
        raise HTTPException(status_code=404, detail="订单不存在")

    # 续住关联组：扣押金同样认 deposit_holder，与 collect/return 对称。
    if order.stay_group_id:
        from app.services import stay_group as stay_group_svc
        _holder = await stay_group_svc.deposit_holder(db, order)
        if _holder.order_id != order.order_id:
            raise HTTPException(status_code=400,
                detail=f"续住组押金挂在 {_holder.order_id} 段，请到该段办理扣留。")

    if order.deposit_status != DepositStatus.collected:
        raise HTTPException(status_code=400, detail=f"当前押金状态为 {order.deposit_status.value}，需先收取后才能扣留")

    order.deposit_status = DepositStatus.withheld
    # Store reason in metadata
    meta = dict(order.metadata_) if order.metadata_ else {}
    meta["deposit_withhold_reason"] = body.reason
    order.metadata_ = meta

    await db.flush()
    refreshed = (await db.execute(
        select(Order).where(Order.order_id == order_id).options(selectinload(Order.rooms))
    )).scalar_one()
    snap = order_snapshot(refreshed)
    snap["_diff"] = {"reason": body.reason}
    await log_action_tx(
        db, current_user["user_id"], "deposit.withhold", "order", order_id,
        after_data=snap,
    )
    await db.commit()
    return {"message": "押金已标记为扣留", "deposit_status": DepositStatus.withheld.value}
