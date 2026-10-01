from fastapi import APIRouter, HTTPException, Query, Response
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload
from typing import Optional
from decimal import Decimal
from datetime import date, datetime
from dataclasses import dataclass
import json
import hashlib
from pydantic import BaseModel, field_validator
import uuid

from app.core.deps import DBSession, CurrentUser
from app.core.datetime_helpers import today_cn
from app.models.settlement import OwnerSettlement, OwnerSettlementItem, SettlementStatus
from app.models.company_sponsored_stay import (
    CompanySponsoredStay,
    CompanySponsorshipStatus,
)
from app.models.room import Room
from app.models.owner import Owner
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.monthly_close import MonthlyCloseCycle
from app.models.expense import Expense, ExpenseCategory, EXPENSE_CATEGORY_LABELS
from app.services.audit import log_action_tx
from app.services.owner_settlement import (
    compute_room_month_owner_stat,
    compute_owner_level_expenses,
    item_precise_owner_net,
    load_room_sponsorship_income,
    settlement_current_amounts,
    settlement_precise_amounts,
)
from app.services import service_fee_ledger
from app.services.service_fee_reconciliation import (
    apply_service_fee_reconciliation,
    plan_service_fee_reconciliation,
)
from app.services.settlement_preflight import run_settlement_preflight
from app.services.monthly_close.financial_lock import acquire_month_financial_lock
from app.services.monthly_close.migration import (
    MonthlyCloseRolloutError,
    require_cycle_write_control,
)
from app.services.monthly_close.workflow import get_or_create_cycle

router = APIRouter(prefix="/settlements", tags=["settlements"])

SETTLEMENT_RULESET_VERSION = "owner-settlement-rules-v3"
SETTLEMENT_CALCULATION_VERSION = "owner-settlement-engine-v2"


def _settlement_digest(value) -> str:
    return hashlib.sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _money(value) -> str:
    return format(Decimal(str(value or 0)).quantize(Decimal("0.01")), ".2f")


async def _lock_settlement_with_owner(db, settlement_id: str):
    """Lock one settlement using the global owner -> settlement lock order."""
    owner_id = await db.scalar(
        select(OwnerSettlement.owner_id).where(
            OwnerSettlement.settlement_id == settlement_id
        )
    )
    if owner_id is None:
        return None
    await service_fee_ledger.lock_owner_service_fee_ledger(db, owner_id)
    return (
        await db.execute(
            select(OwnerSettlement)
            .where(OwnerSettlement.settlement_id == settlement_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()


def _unresolved_service_fee_detail(entry) -> dict:
    """Expose only stable business identifiers from an unresolved plan entry."""
    return {
        "reason": entry.reason,
        "order_id": entry.order_id,
        "room_id": entry.room_id,
        "stay_group_id": entry.stay_group_id,
        "order_ids": list(entry.order_ids),
        "room_ids": list(entry.room_ids),
    }


def _service_fee_confirmation_error(
    settlement: OwnerSettlement,
    *,
    missing: list[dict] | None = None,
    unresolved: list[dict] | None = None,
    action: str,
) -> HTTPException:
    """Build the public, actionable fail-closed confirmation response."""
    missing = missing or []
    unresolved = unresolved or []
    return HTTPException(
        status_code=409,
        detail={
            "code": "service_fee_ledger_incomplete",
            "message": "结算服务费账本不完整，暂不能确认",
            "owner_id": settlement.owner_id,
            "billing_month": settlement.billing_month,
            "missing_count": len(missing),
            "unresolved_count": len(unresolved),
            "missing": missing,
            "unresolved": unresolved,
            "action": action,
        },
    )


class SettlementOut(BaseModel):
    settlement_id: str
    owner_id: str
    billing_month: str
    total_net_revenue: Decimal
    owner_amount: Decimal
    deducted_expenses: Decimal
    actual_owner_amount: Decimal
    # 账面「到厘」展示值（打款仍用上面到分的 owner_amount/actual_owner_amount）。
    # 从明细逐房 net_revenue×share_ratio 到 0.001 重算，见 owner_settlement.py。
    owner_amount_precise: Optional[Decimal] = None
    actual_owner_amount_precise: Optional[Decimal] = None
    status: SettlementStatus
    notes: Optional[str] = None
    created_at: str

    model_config = {"from_attributes": True}

    @field_validator("created_at", mode="before")
    @classmethod
    def _coerce_created_at(cls, v):
        # 列表接口直接把 ORM 对象交给 response_model 序列化，created_at 是 datetime；
        # 若不转成字符串，Pydantic v2 会拒绝 datetime->str 使整个列表请求 500。
        if isinstance(v, datetime):
            return v.isoformat()
        return v


class SettlementItemOut(BaseModel):
    item_id: str
    room_id: Optional[str] = None
    room_name: Optional[str] = None
    label: Optional[str] = None
    order_count: int
    revenue: Decimal
    commission: Decimal
    net_revenue: Decimal
    externally_settled_income: Decimal
    sponsorship_adjustment_id: Optional[str] = None
    owner_expenses: Decimal
    share_ratio_snapshot: Decimal
    owner_net_amount: Decimal
    # 该房「业主应得·到厘」展示值（打款仍用 owner_net_amount 到分值）。
    owner_net_amount_precise: Optional[Decimal] = None

    model_config = {"from_attributes": True}


class SettlementDetailOut(SettlementOut):
    items: list[SettlementItemOut] = []


class DisputeBody(BaseModel):
    notes: str


@dataclass(frozen=True)
class SettlementItemSnapshot:
    room_id: str | None
    label: str | None
    order_count: int
    revenue: Decimal
    commission: Decimal
    net_revenue: Decimal
    externally_settled_income: Decimal
    owner_expenses: Decimal
    share_ratio_snapshot: Decimal
    owner_net_amount: Decimal
    cost_share_breakdown: list


@dataclass(frozen=True)
class SettlementFinancialSnapshot:
    total_revenue: Decimal
    total_net_revenue: Decimal
    owner_amount: Decimal
    deducted_expenses: Decimal
    actual_owner_amount: Decimal
    items: tuple[SettlementItemSnapshot, ...]


def _canonical_settlement_item_facts(
    snapshot: SettlementFinancialSnapshot,
    service_fee_refs: dict[str, str],
) -> list[dict]:
    """Normalize calculator output into the immutable approval representation."""
    return [
        {
            "room_id": item.room_id,
            "label": item.label,
            "order_count": int(item.order_count),
            "revenue": _money(item.revenue),
            "commission": _money(item.commission),
            "net_revenue": _money(item.net_revenue),
            "externally_settled_income": _money(item.externally_settled_income),
            "owner_expenses": _money(item.owner_expenses),
            "share_ratio_snapshot": format(
                Decimal(str(item.share_ratio_snapshot or 0)).quantize(
                    Decimal("0.001")
                ),
                ".3f",
            ),
            "owner_net_amount": _money(item.owner_net_amount),
            "cost_share_breakdown": _canonical_cost_share_breakdown(
                item.cost_share_breakdown, service_fee_refs
            ),
        }
        for item in snapshot.items
    ]


def _canonical_cost_share_breakdown(
    breakdown: list | None, service_fee_refs: dict[str, str]
) -> list:
    entries = [
        (
            {
                **{
                    key: value
                    for key, value in entry.items()
                    if key != "expense_id"
                },
                "service_fee_key": service_fee_refs[entry["expense_id"]],
            }
            if entry.get("expense_id") in service_fee_refs
            else entry
        )
        for entry in (breakdown or [])
    ]

    # JSON object keys are sorted separately; expense list order is not financial state.
    return sorted(entries, key=lambda entry: json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str))


def _sponsorship_binding_snapshot(
    income,
    *,
    room_id: str,
    settlement_item_id: str,
) -> dict:
    root = income.root
    return {
        "sponsorship_id": root.sponsored_stay_id,
        "settlement_item_id": settlement_item_id,
        "room_id": room_id,
        "source_order_id": root.source_order_id,
        "segment_order_id": root.segment_order_id,
        "effective_amount": _money(income.amount),
        "payment_responsibility": getattr(
            root.payment_responsibility, "value", root.payment_responsibility
        ),
        "status": getattr(root.status, "value", root.status),
        "version": int(root.version),
        "source_price_snapshot_id": root.source_price_snapshot_id,
    }


async def _existing_settlement_snapshot(
    db,
    settlement: OwnerSettlement,
    year: int,
    month: int,
) -> dict:
    """Bind every current fact that makes preserving a settlement safe."""
    period_start = date(year, month, 1)
    period_end = (
        date(year + 1, 1, 1)
        if month == 12
        else date(year, month + 1, 1)
    )
    rooms = list(
        await db.scalars(
            select(Room)
            .where(Room.owner_id == settlement.owner_id)
            .order_by(Room.room_id)
        )
    )
    room_ids = [room.room_id for room in rooms]
    order_rows = list(
        (
            await db.execute(
                select(Order, OrderRoom)
                .join(OrderRoom, OrderRoom.order_id == Order.order_id)
                .where(
                    Order.is_deleted.is_(False),
                    Order.order_status != OrderStatus.cancelled,
                    OrderRoom.room_id.in_(room_ids),
                    OrderRoom.check_out_date >= period_start,
                    OrderRoom.check_out_date < period_end,
                )
                .order_by(Order.order_id, OrderRoom.position, OrderRoom.order_room_id)
            )
        ).all()
    ) if room_ids else []
    expenses = list(
        await db.scalars(
            select(Expense)
            .where(
                Expense.owner_id == settlement.owner_id,
                Expense.is_deleted.is_(False),
                Expense.expense_date >= period_start,
                Expense.expense_date < period_end,
            )
            .order_by(Expense.expense_id)
        )
    )
    service_fee_refs = {
        expense.expense_id: (
            f"{expense.order_id}:{expense.room_id}:{expense.category.value}"
        )
        for expense in expenses
        if expense.is_service_fee
    }
    persisted_items = list(
        await db.scalars(
            select(OwnerSettlementItem)
            .where(OwnerSettlementItem.settlement_id == settlement.settlement_id)
            .order_by(OwnerSettlementItem.item_id)
        )
    )
    current = await _build_settlement_financial_snapshot(
        db, settlement.owner_id, year, month, rooms=rooms
    )
    sponsorship_by_room = await load_room_sponsorship_income(
        db, room_ids, year, month, for_update=True
    )
    sponsorship_bindings = sorted(
        (
            {
                **_sponsorship_binding_snapshot(
                    income,
                    room_id=room_id,
                    settlement_item_id=income.root.settlement_item_id,
                ),
                "settlement_batch_id": income.root.settlement_batch_id,
            }
            for room_id in room_ids
            for income in sponsorship_by_room.get(room_id, [])
        ),
        key=lambda item: (
            item["sponsorship_id"],
            item.get("settlement_batch_id") or "",
            item.get("settlement_item_id") or "",
        ),
    )
    return {
        "settlement_id": settlement.settlement_id,
        "owner_id": settlement.owner_id,
        "status": getattr(settlement.status, "value", settlement.status),
        "header": {
            "settlement_id": settlement.settlement_id,
            "owner_id": settlement.owner_id,
            "billing_month": settlement.billing_month,
            "total_net_revenue": _money(settlement.total_net_revenue),
            "owner_amount": _money(settlement.owner_amount),
            "deducted_expenses": _money(settlement.deducted_expenses),
            "actual_owner_amount": _money(settlement.actual_owner_amount),
            "status": getattr(settlement.status, "value", settlement.status),
            "payment_date": (
                settlement.payment_date.isoformat()
                if settlement.payment_date is not None
                else None
            ),
            "doc_url": settlement.doc_url,
            "notes": settlement.notes,
            "created_by": settlement.created_by,
        },
        "room_ids": room_ids,
        "order_ids": sorted({order.order_id for order, _segment in order_rows}),
        "order_room_ids": [segment.order_room_id for _order, segment in order_rows],
        "item_ids": [item.item_id for item in persisted_items],
        "items": [
            {
                "item_id": item.item_id,
                "settlement_id": item.settlement_id,
                "room_id": item.room_id,
                "label": item.label,
                "order_room_id": item.order_room_id,
                "order_count": int(item.order_count),
                "revenue": _money(item.revenue),
                "commission": _money(item.commission),
                "net_revenue": _money(item.net_revenue),
                "externally_settled_income": _money(
                    item.externally_settled_income
                ),
                "sponsorship_adjustment_id": item.sponsorship_adjustment_id,
                "owner_expenses": _money(item.owner_expenses),
                "share_ratio_snapshot": format(
                    Decimal(str(item.share_ratio_snapshot or 0)).quantize(
                        Decimal("0.001")
                    ),
                    ".3f",
                ),
                "owner_net_amount": _money(item.owner_net_amount),
                "cost_share_breakdown": _canonical_cost_share_breakdown(
                    item.cost_share_breakdown, service_fee_refs
                ),
            }
            for item in persisted_items
        ],
        "expenses": [
            {
                "expense_id": (
                    None if expense.is_service_fee else expense.expense_id
                ),
                "service_fee_key": service_fee_refs.get(expense.expense_id),
                "category": expense.category.value,
                "amount": _money(expense.amount),
                "description": expense.description,
                "expense_date": expense.expense_date.isoformat(),
                "room_id": expense.room_id,
                "order_id": expense.order_id,
                "payer": getattr(expense.payer, "value", expense.payer),
                "owner_id": expense.owner_id,
                "is_service_fee": bool(expense.is_service_fee),
            }
            for expense in expenses
        ],
        "share_ratio_versions": {
            room.room_id: format(
                Decimal(str(room.owner_share_ratio or 0)).quantize(
                    Decimal("0.001")
                ),
                ".3f",
            )
            for room in rooms
        },
        "service_fee_keys": sorted(service_fee_refs.values()),
        "sponsorship_bindings": sponsorship_bindings,
        "current_financial_snapshot": {
            "total_revenue": _money(current.total_revenue),
            "total_net_revenue": _money(current.total_net_revenue),
            "owner_amount": _money(current.owner_amount),
            "deducted_expenses": _money(current.deducted_expenses),
            "actual_owner_amount": _money(current.actual_owner_amount),
            "items": _canonical_settlement_item_facts(current, service_fee_refs),
        },
    }


async def _build_settlement_financial_snapshot(
    db,
    owner_id: str,
    year: int,
    month: int,
    *,
    rooms: list[Room] | None = None,
) -> SettlementFinancialSnapshot:
    """Recompute one owner/month using the generation accounting sources."""
    if rooms is None:
        rooms = list(
            (
                await db.execute(
                    select(Room).where(Room.owner_id == owner_id)
                )
            ).scalars()
        )

    items: list[SettlementItemSnapshot] = []
    total_revenue = Decimal("0")
    total_net_revenue = Decimal("0")
    total_owner_expenses = Decimal("0")
    total_owner_revenue_share = Decimal("0")
    total_owner_net = Decimal("0")
    sponsorship_income_by_room = await load_room_sponsorship_income(
        db, [room.room_id for room in rooms], year, month, for_update=True
    )
    for room in rooms:
        stat = await compute_room_month_owner_stat(
            db,
            room,
            year,
            month,
            sponsorship_income_by_room=sponsorship_income_by_room,
        )
        item_net_revenue = stat.net_revenue.quantize(Decimal("0.01"))
        items.append(
            SettlementItemSnapshot(
                room_id=room.room_id,
                label=None,
                order_count=stat.order_count,
                revenue=stat.revenue.quantize(Decimal("0.01")),
                commission=stat.commission.quantize(Decimal("0.01")),
                net_revenue=item_net_revenue,
                externally_settled_income=stat.externally_settled_income.quantize(
                    Decimal("0.01")
                ),
                owner_expenses=stat.owner_expenses.quantize(Decimal("0.01")),
                share_ratio_snapshot=stat.share_ratio,
                owner_net_amount=stat.owner_net,
                cost_share_breakdown=stat.cost_share_breakdown,
            )
        )
        total_revenue += stat.revenue
        total_net_revenue += item_net_revenue
        total_owner_expenses += stat.owner_expenses
        total_owner_revenue_share += stat.owner_revenue_share
        total_owner_net += stat.owner_net

    owner_level = await compute_owner_level_expenses(db, owner_id, year, month)
    for entry in owner_level.breakdown:
        amount = Decimal(entry["amount"])
        category = ExpenseCategory(entry["category"])
        label = EXPENSE_CATEGORY_LABELS.get(category, entry["category"])
        items.append(
            SettlementItemSnapshot(
                room_id=None,
                label=label,
                order_count=0,
                revenue=Decimal("0.00"),
                commission=Decimal("0.00"),
                net_revenue=Decimal("0.00"),
                externally_settled_income=Decimal("0.00"),
                owner_expenses=amount,
                share_ratio_snapshot=Decimal("0"),
                owner_net_amount=(-amount).quantize(Decimal("0.01")),
                cost_share_breakdown=[entry],
            )
        )
        total_owner_expenses += amount
        total_owner_net -= amount

    return SettlementFinancialSnapshot(
        total_revenue=total_revenue,
        total_net_revenue=total_net_revenue.quantize(Decimal("0.01")),
        owner_amount=total_owner_revenue_share.quantize(Decimal("0.01")),
        deducted_expenses=total_owner_expenses.quantize(Decimal("0.01")),
        actual_owner_amount=total_owner_net.quantize(Decimal("0.01")),
        items=tuple(items),
    )


def _money_fingerprint(value) -> str:
    return str(Decimal(str(value or 0)).quantize(Decimal("0.01")))


def _item_fingerprint(item, service_fee_refs: dict[str, str] | None = None) -> tuple:
    cost_share_breakdown = _canonical_cost_share_breakdown(
        item.cost_share_breakdown, service_fee_refs or {}
    )
    return (
        item.room_id or "",
        getattr(item, "label", None) or "",
        int(item.order_count),
        _money_fingerprint(item.revenue),
        _money_fingerprint(item.commission),
        _money_fingerprint(item.net_revenue),
        _money_fingerprint(item.owner_expenses),
        str(
            Decimal(str(item.share_ratio_snapshot or 0)).quantize(
                Decimal("0.001")
            )
        ),
        _money_fingerprint(item.owner_net_amount),
        json.dumps(
            cost_share_breakdown,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ),
    )


def _financial_snapshot_drift(
    settlement: OwnerSettlement,
    stored_items: list[OwnerSettlementItem],
    current: SettlementFinancialSnapshot,
    *,
    service_fee_refs: dict[str, str] | None = None,
) -> dict | None:
    settlement_fields = [
        field
        for field in (
            "total_net_revenue",
            "owner_amount",
            "deducted_expenses",
            "actual_owner_amount",
        )
        if _money_fingerprint(getattr(settlement, field))
        != _money_fingerprint(getattr(current, field))
    ]
    stored_fingerprint = sorted(
        _item_fingerprint(item, service_fee_refs) for item in stored_items
    )
    current_fingerprint = sorted(
        _item_fingerprint(item, service_fee_refs) for item in current.items
    )
    items_changed = stored_fingerprint != current_fingerprint
    if not settlement_fields and not items_changed:
        return None
    return {
        "settlement_fields": settlement_fields,
        "items_changed": items_changed,
        "stored_item_count": len(stored_items),
        "current_item_count": len(current.items),
    }


@router.get("", response_model=list[SettlementOut])
async def list_settlements(
    db: DBSession,
    current_user: CurrentUser,
    owner_id: Optional[str] = Query(default=None),
    billing_month: Optional[str] = Query(default=None),
):
    """List settlements. Owners see only their own; admin/finance see all."""
    q = select(OwnerSettlement)
    if current_user["role"] not in ("admin", "finance", "owner"):
        raise HTTPException(status_code=403, detail="无权查看结算")
    if owner_id:
        q = q.where(OwnerSettlement.owner_id == owner_id)

    if billing_month:
        q = q.where(OwnerSettlement.billing_month == billing_month)

    q = q.order_by(OwnerSettlement.billing_month.desc())
    q = q.options(selectinload(OwnerSettlement.items))
    result = await db.execute(q)
    settlements = result.scalars().all()
    response: list[SettlementOut] = []
    for s in settlements:
        owner_amount_p, actual_p = settlement_precise_amounts(s.items)
        current = settlement_current_amounts(s)
        response.append(
            SettlementOut(
                settlement_id=s.settlement_id,
                owner_id=s.owner_id,
                billing_month=s.billing_month,
                total_net_revenue=current.total_net_revenue,
                owner_amount=current.owner_amount,
                deducted_expenses=current.deducted_expenses,
                actual_owner_amount=current.actual_owner_amount,
                owner_amount_precise=owner_amount_p,
                actual_owner_amount_precise=actual_p,
                status=s.status,
                notes=s.notes,
                created_at=s.created_at,
            )
        )
    return response


async def build_settlement_plan_snapshot(
    db,
    year: int,
    month: int,
    *,
    overwrite: bool = False,
) -> dict:
    """Build the immutable financial and identity set approved by month close.

    Missing deterministic service-fee rows are applied only in a savepoint so
    the existing settlement calculator sees the exact future ledger while the
    proposal builder itself leaves no business mutation behind.
    """

    current_date = today_cn()
    if (year, month) >= (current_date.year, current_date.month):
        raise HTTPException(
            status_code=409,
            detail=(
                "正式结算只能生成已结束的自然月；"
                f"北京日期 {current_date.isoformat()} 尚未关闭 {year:04d}-{month:02d}"
            ),
        )
    billing_month = f"{year:04d}-{month:02d}"
    preflight = await run_settlement_preflight(db, year, month)
    if preflight.blocking:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "settlement_preflight_failed",
                "message": "月结体检发现未处理异常，请处理完成后再生成结算。",
                "report": preflight.to_dict(),
            },
        )
    preflight_snapshot = preflight.gate_dict()
    preflight_hash = _settlement_digest(preflight_snapshot)
    owners = list(await db.scalars(select(Owner).order_by(Owner.owner_id)))
    existing_rows = list(
        await db.scalars(
            select(OwnerSettlement)
            .where(OwnerSettlement.billing_month == billing_month)
            .order_by(OwnerSettlement.owner_id, OwnerSettlement.settlement_id)
        )
    )
    existing_by_owner = {row.owner_id: row for row in existing_rows}
    plans: list[dict] = []
    fee_plans = {}
    nested = await db.begin_nested()
    try:
        for owner in owners:
            existing = existing_by_owner.get(owner.owner_id)
            if existing is not None and (
                not overwrite or existing.status != SettlementStatus.pending
            ):
                continue
            rooms = list(
                await db.scalars(
                    select(Room)
                    .where(Room.owner_id == owner.owner_id)
                    .order_by(Room.room_id)
                )
            )
            if not rooms:
                continue
            fee_plan = await plan_service_fee_reconciliation(
                db, owner.owner_id, year, month
            )
            if fee_plan.unresolved:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "service_fee_reconciliation_unresolved",
                        "message": "服务费仍有无法确定的订单，不能生成结算方案。",
                        "owner_id": owner.owner_id,
                        "unresolved": [
                            _unresolved_service_fee_detail(item)
                            for item in fee_plan.unresolved
                        ],
                    },
                )
            fee_plans[owner.owner_id] = fee_plan
            await apply_service_fee_reconciliation(db, fee_plan, operator_id=None)
            snapshot = await _build_settlement_financial_snapshot(
                db, owner.owner_id, year, month, rooms=rooms
            )
            service_fee_refs = {
                row.expense_id: (
                    f"{row.order_id}:{row.room_id}:{row.category.value}"
                )
                for row in await db.scalars(
                    select(Expense).where(
                        Expense.owner_id == owner.owner_id,
                        Expense.is_service_fee.is_(True),
                        Expense.is_deleted.is_(False),
                        Expense.expense_date >= date(year, month, 1),
                        Expense.expense_date
                        < (
                            date(year + 1, 1, 1)
                            if month == 12
                            else date(year, month + 1, 1)
                        ),
                    )
                )
            }
            if snapshot.total_net_revenue == 0 and snapshot.deducted_expenses == 0:
                continue
            room_ids = sorted(room.room_id for room in rooms)
            order_ids = sorted(
                set(
                    await db.scalars(
                        select(Order.order_id)
                        .join(OrderRoom, OrderRoom.order_id == Order.order_id)
                        .where(
                            Order.is_deleted.is_(False),
                            Order.order_status != OrderStatus.cancelled,
                            OrderRoom.room_id.in_(room_ids),
                            OrderRoom.check_out_date >= date(year, month, 1),
                            OrderRoom.check_out_date
                            < (
                                date(year + 1, 1, 1)
                                if month == 12
                                else date(year, month + 1, 1)
                            ),
                        )
                    )
                )
            )
            item_facts = _canonical_settlement_item_facts(
                snapshot, service_fee_refs
            )
            identity_hash = _settlement_digest(
                {
                    "billing_month": billing_month,
                    "owner_id": owner.owner_id,
                    "items": item_facts,
                    "order_ids": order_ids,
                    "overwrite": overwrite,
                }
            )
            settlement_id = f"STL-{identity_hash[:12].upper()}"
            item_ids = [
                f"SLI-{_settlement_digest({'settlement_id': settlement_id, 'index': index, 'item': item})[:12].upper()}"
                for index, item in enumerate(item_facts)
            ]
            room_item_ids = {
                item["room_id"]: item_id
                for item_id, item in zip(item_ids, item_facts)
                if item["room_id"] is not None
            }
            sponsorship_income_by_room = await load_room_sponsorship_income(
                db, room_ids, year, month, for_update=True
            )
            sponsorship_bindings = sorted(
                (
                    _sponsorship_binding_snapshot(
                        income,
                        room_id=room_id,
                        settlement_item_id=room_item_ids[room_id],
                    )
                    for room_id in room_ids
                    for income in sponsorship_income_by_room.get(room_id, [])
                ),
                key=lambda item: (
                    item["sponsorship_id"],
                    item["settlement_item_id"],
                ),
            )
            plans.append(
                {
                    "settlement_id": settlement_id,
                    "owner_id": owner.owner_id,
                    "status": SettlementStatus.pending.value,
                    "room_ids": room_ids,
                    "order_ids": order_ids,
                    "item_ids": item_ids,
                    "items": item_facts,
                    "sponsorship_bindings": sponsorship_bindings,
                    "total_revenue": _money(snapshot.total_revenue),
                    "total_net_revenue": _money(snapshot.total_net_revenue),
                    "owner_amount": _money(snapshot.owner_amount),
                    "deducted_expenses": _money(snapshot.deducted_expenses),
                    "actual_owner_amount": _money(snapshot.actual_owner_amount),
                    "share_ratio_versions": {
                        room.room_id: format(
                            Decimal(str(room.owner_share_ratio or 0)).quantize(
                                Decimal("0.001")
                            ),
                            ".3f",
                        )
                        for room in rooms
                    },
                    "service_fee_keys": sorted(
                        f"{item.order_id}:{item.room_id}:{item.category.value}"
                        for item in fee_plan.expected
                    ),
                    "replaces_settlement_id": (
                        existing.settlement_id if existing is not None else None
                    ),
                }
            )
    finally:
        await nested.rollback()

    plans.sort(key=lambda item: (item["owner_id"], item["settlement_id"]))
    replaced_settlement_ids = {
        item["replaces_settlement_id"]
        for item in plans
        if item["replaces_settlement_id"] is not None
    }
    preserved_settlements = [
        await _existing_settlement_snapshot(db, row, year, month)
        for row in existing_rows
        if row.settlement_id not in replaced_settlement_ids
    ]
    locked_settlements = [
        item
        for item in preserved_settlements
        if item["status"] != SettlementStatus.pending.value
    ]
    total_amount = sum(
        (Decimal(item["actual_owner_amount"]) for item in plans), Decimal("0.00")
    )
    result = {
        "billing_month": billing_month,
        "overwrite": overwrite,
        "ruleset_version": SETTLEMENT_RULESET_VERSION,
        "calculation_version": SETTLEMENT_CALCULATION_VERSION,
        "preflight_hash": preflight_hash,
        "preflight": preflight_snapshot,
        "owner_ids": [item["owner_id"] for item in plans],
        "settlement_ids": [item["settlement_id"] for item in plans],
        "settlements": plans,
        "locked_settlements": locked_settlements,
        "preserved_settlements": preserved_settlements,
        "total_amount": _money(total_amount),
    }
    result["plan_hash"] = _settlement_digest(result)
    return result


async def _generate_settlements_core(
    db, year: int, month: int, created_by: Optional[str] = None,
    overwrite: bool = False,
    *,
    expected_cycle_id: str,
    expected_write_owner: str,
    expected_control_version: int,
    commit: bool = True,
    approved_plan: dict | None = None,
) -> dict:
    """
    按房号维度生成业主月度结算单（含子明细）。
    - 遍历每位业主名下每套房
    - 每套房独立应用 share_ratio 与 deduction_rules
    - snapshot 当时的 ratio 到 OwnerSettlementItem，保证历史完整性
    - 已存在的 (owner_id, billing_month)：
        * overwrite=False（默认）→ 跳过，不重复生成（幂等）。
        * overwrite=True → 仅 pending（待确认）删旧重算；confirmed/paid/disputed
          一律保护、计入 skipped_locked，避免抹掉已认账/已打款记录。
    """
    current_date = today_cn()
    if (year, month) >= (current_date.year, current_date.month):
        raise HTTPException(
            status_code=409,
            detail=(
                "正式结算只能生成已结束的自然月；"
                f"北京日期 {current_date.isoformat()} 尚未关闭 {year:04d}-{month:02d}"
            ),
        )

    billing_month = f"{year}-{str(month).zfill(2)}"
    await acquire_month_financial_lock(db, billing_month)
    locked_cycle = await require_cycle_write_control(
        db,
        expected_cycle_id,
        expected_version=expected_control_version,
        owner=expected_write_owner,
    )
    if locked_cycle.billing_month != billing_month:
        raise MonthlyCloseRolloutError(
            "write_control_cycle_mismatch",
            "结算月份与写入控制周期不一致，请刷新后重试",
        )
    planned_by_owner: dict[str, dict] = {}
    if approved_plan is not None:
        fresh_plan = await build_settlement_plan_snapshot(
            db, year, month, overwrite=overwrite
        )
        if fresh_plan != approved_plan:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": "settlement_plan_stale",
                    "message": "结算事实已经变化，请重新生成并审批方案。",
                    "current_plan_hash": fresh_plan.get("plan_hash"),
                },
            )
        planned_by_owner = {
            item["owner_id"]: item for item in approved_plan["settlements"]
        }

    # 生成前先做全月只读体检。必须放在删除旧 pending 结算之前，确保失败时不产生
    # “旧单已删、新单没生成”的半成品状态。
    preflight = await run_settlement_preflight(db, year, month)
    if preflight.blocking:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "settlement_preflight_failed",
                "message": "月结体检发现未处理异常，请处理完成后再生成结算。",
                "report": preflight.to_dict(),
            },
        )

    owners_result = await db.execute(select(Owner).order_by(Owner.owner_id))
    owners = owners_result.scalars().all()

    generated = 0
    regenerated = 0
    skipped_locked = 0
    service_fees_created = 0
    service_fees_amount = Decimal("0")
    blocked_unresolved: list[dict] = []
    for owner in owners:
        # One stable owner row serializes settlement creation, fee repair, and
        # settlement status transitions. Always take it before a settlement row.
        await service_fee_ledger.lock_owner_service_fee_ledger(db, owner.owner_id)
        existing = (await db.execute(
            select(OwnerSettlement).where(
                OwnerSettlement.owner_id == owner.owner_id,
                OwnerSettlement.billing_month == billing_month,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )).scalar_one_or_none()
        is_regen = False
        if existing is not None:
            if not overwrite:
                continue
            if existing.status != SettlementStatus.pending:
                # 已确认/已打款/有争议：保护,不覆盖
                skipped_locked += 1
                continue
            bound_sponsorships = await db.scalar(
                select(func.count(CompanySponsoredStay.sponsored_stay_id)).where(
                    CompanySponsoredStay.settlement_batch_id
                    == existing.settlement_id
                )
            )
            if bound_sponsorships:
                # Sponsorship roots become settled when the draft is generated and
                # are immutable thereafter. A correction is another delta, not a
                # delete/rebind of the settled history.
                skipped_locked += 1
                continue
            # 待确认单可覆盖；实际删除延后到对账通过之后。
            is_regen = True

        rooms_result = await db.execute(
            select(Room).where(Room.owner_id == owner.owner_id)
        )
        rooms = rooms_result.scalars().all()
        if not rooms:
            continue

        fee_plan = await plan_service_fee_reconciliation(
            db, owner.owner_id, year, month
        )
        if fee_plan.unresolved:
            blocked_unresolved.append(
                {
                    "owner_id": owner.owner_id,
                    "unresolved": [
                        {
                            "reason": entry.reason,
                            "order_id": entry.order_id,
                            "room_id": entry.room_id,
                            "stay_group_id": entry.stay_group_id,
                            "order_ids": list(entry.order_ids),
                            "room_ids": list(entry.room_ids),
                        }
                        for entry in fee_plan.unresolved
                    ],
                }
            )
            continue

        if is_regen:
            # Status is repeated in the DELETE as a final compare-and-delete gate.
            # Parent-first is safe in PostgreSQL (ON DELETE CASCADE); the explicit
            # item cleanup also keeps SQLite tests and legacy schemas tidy.
            deleted = await db.execute(
                delete(OwnerSettlement).where(
                    OwnerSettlement.settlement_id == existing.settlement_id,
                    OwnerSettlement.status == SettlementStatus.pending,
                )
            )
            if (deleted.rowcount or 0) != 1:
                skipped_locked += 1
                continue
            await db.execute(
                delete(OwnerSettlementItem).where(
                    OwnerSettlementItem.settlement_id == existing.settlement_id
                )
            )

        repair = await apply_service_fee_reconciliation(
            db, fee_plan, operator_id=created_by
        )
        service_fees_created += repair.created_count
        service_fees_amount += repair.created_amount

        snapshot = await _build_settlement_financial_snapshot(
            db, owner.owner_id, year, month, rooms=rooms
        )
        # 跳过无任何收入且无支出的业主（避免噪音空单）
        if snapshot.total_net_revenue == 0 and snapshot.deducted_expenses == 0:
            continue

        planned = planned_by_owner.get(owner.owner_id)
        settlement_id = (
            planned["settlement_id"]
            if planned is not None
            else "STL-" + uuid.uuid4().hex[:12].upper()
        )
        settlement = OwnerSettlement(
            settlement_id=settlement_id,
            owner_id=owner.owner_id,
            billing_month=billing_month,
            total_net_revenue=snapshot.total_net_revenue,
            owner_amount=snapshot.owner_amount,
            deducted_expenses=snapshot.deducted_expenses,
            actual_owner_amount=snapshot.actual_owner_amount,
            status=SettlementStatus.pending,
            created_by=created_by,
        )
        db.add(settlement)
        sponsorship_bindings: list[tuple[object, OwnerSettlementItem]] = []
        sponsorship_income_by_room = await load_room_sponsorship_income(
            db, [room.room_id for room in rooms], year, month, for_update=True
        )
        for item_index, item in enumerate(snapshot.items):
            settlement_item = OwnerSettlementItem(
                item_id=(
                    planned["item_ids"][item_index]
                    if planned is not None
                    else "SLI-" + uuid.uuid4().hex[:12].upper()
                ),
                settlement_id=settlement_id,
                room_id=item.room_id,
                label=item.label,
                order_count=item.order_count,
                revenue=item.revenue,
                commission=item.commission,
                net_revenue=item.net_revenue,
                externally_settled_income=item.externally_settled_income,
                owner_expenses=item.owner_expenses,
                share_ratio_snapshot=item.share_ratio_snapshot,
                owner_net_amount=item.owner_net_amount,
                cost_share_breakdown=(
                    planned["items"][item_index]["cost_share_breakdown"]
                    if planned is not None
                    else item.cost_share_breakdown
                ),
            )
            db.add(settlement_item)
            if item.room_id is not None:
                for income in sponsorship_income_by_room.get(item.room_id, []):
                    sponsorship_bindings.append((income, settlement_item))
        if planned is not None:
            current_bindings = sorted(
                (
                    _sponsorship_binding_snapshot(
                        income,
                        room_id=item.room_id,
                        settlement_item_id=item.item_id,
                    )
                    for income, item in sponsorship_bindings
                ),
                key=lambda entry: (
                    entry["sponsorship_id"],
                    entry["settlement_item_id"],
                ),
            )
            if current_bindings != planned["sponsorship_bindings"]:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "code": "settlement_plan_stale",
                        "message": "公司承担住宿事实已经变化，请重新生成并审批方案。",
                    },
                )
        # Root consistency validation reads the referenced item from the database.
        # Flush the new batch/items first, still inside this single uncommitted
        # transaction, then attach and settle every included sponsorship root.
        await db.flush()
        for income, item in sponsorship_bindings:
            root = income.root
            if root.status != CompanySponsorshipStatus.confirmed:
                raise ValueError(
                    "only confirmed sponsorships may enter a new settlement"
                )
            root.status = CompanySponsorshipStatus.settled
            root.settlement_item_id = item.item_id
            root.settlement_batch_id = settlement_id
            root.updated_by = created_by
        if is_regen:
            regenerated += 1
        else:
            generated += 1

    # 审计与生成同事务提交（写失败则整批回滚）。移进 core 后，worker/admin 自动生成
    # 也一并留痕（operator_id 为 None 即系统任务）。
    await log_action_tx(
        db, created_by,
        "settlement.regenerate" if overwrite else "settlement.generate",
        "settlement", billing_month,
        after_data={
            "generated": generated,
            "regenerated": regenerated,
            "skipped_locked": skipped_locked,
            "service_fees_created": service_fees_created,
            "service_fees_amount": f"{service_fees_amount:.2f}",
            "blocked_unresolved": blocked_unresolved,
        },
    )
    if commit:
        await db.commit()
    else:
        await db.flush()
    return {
        "generated": generated,
        "regenerated": regenerated,
        "skipped_locked": skipped_locked,
        "service_fees_created": service_fees_created,
        "service_fees_amount": f"{service_fees_amount:.2f}",
        "blocked_unresolved": blocked_unresolved,
        "billing_month": billing_month,
    }


@router.post("/generate")
async def generate_settlements(
    db: DBSession,
    current_user: CurrentUser,
    response: Response,
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
    overwrite: bool = Query(
        default=False,
        description="重新生成:覆盖已有的『待确认』单(改了历史订单/第一次算错时用);"
                    "已确认/已打款的单会被保护、计入 skipped_locked。",
    ),
):
    """Generate monthly settlements for all owners. Admin only."""
    if current_user["role"] != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可生成结算")

    billing_month = f"{year:04d}-{month:02d}"
    cycle = await get_or_create_cycle(db, billing_month, current_user["user_id"])
    observed_control_version = cycle.control_version
    if cycle is not None and cycle.write_control_owner == "assistant":
        from app.services.monthly_close.finalization import (
            build_settlement_proposal,
        )

        proposal = await build_settlement_proposal(
            db,
            cycle,
            current_user,
            request_id=f"LEGACY-SETTLEMENT-{cycle.cycle_id}-{int(overwrite)}",
            overwrite=overwrite,
        )
        response.status_code = 202
        return {
            "proposal_id": proposal.proposal_id,
            "proposal_type": proposal.proposal_type,
            "status": proposal.status,
            "cycle_id": proposal.cycle_id,
            "evidence_hash": proposal.evidence_hash,
            "impact_snapshot": proposal.impact_snapshot,
        }

    try:
        result = await _generate_settlements_core(
            db,
            year,
            month,
            current_user["user_id"],
            overwrite=overwrite,
            expected_cycle_id=cycle.cycle_id,
            expected_write_owner="legacy",
            expected_control_version=observed_control_version,
        )
    except MonthlyCloseRolloutError as exc:
        await db.rollback()
        raise HTTPException(status_code=409, detail=exc.to_detail()) from exc
    return result


@router.get("/preflight")
async def settlement_preflight(
    db: DBSession,
    current_user: CurrentUser,
    year: int = Query(...),
    month: int = Query(..., ge=1, le=12),
):
    """月结前只读体检；供结算页展示异常清单。"""
    if current_user["role"] not in ("admin", "finance"):
        raise HTTPException(status_code=403, detail="无权查看月结体检")
    report = await run_settlement_preflight(db, year, month)
    return report.to_dict()


@router.get("/{settlement_id}", response_model=SettlementDetailOut)
async def get_settlement_detail(
    settlement_id: str, db: DBSession, current_user: CurrentUser,
):
    """结算单详情 + 每套房明细"""
    if current_user["role"] not in ("admin", "finance", "owner"):
        raise HTTPException(status_code=403, detail="无权查看结算")

    result = await db.execute(
        select(OwnerSettlement)
        .options(selectinload(OwnerSettlement.items))
        .where(OwnerSettlement.settlement_id == settlement_id)
    )
    settlement = result.scalar_one_or_none()
    if not settlement:
        raise HTTPException(status_code=404, detail="结算记录不存在")

    # 加载房名（业主级支出行 room_id 为空，跳过）
    room_ids = [i.room_id for i in settlement.items if i.room_id]
    rooms_map: dict[str, str] = {}
    if room_ids:
        rooms_result = await db.execute(
            select(Room.room_id, Room.room_name).where(Room.room_id.in_(room_ids))
        )
        rooms_map = {rid: rname for rid, rname in rooms_result.all()}

    items_out = [
        SettlementItemOut(
            item_id=i.item_id,
            room_id=i.room_id,
            room_name=rooms_map.get(i.room_id) if i.room_id else None,
            label=getattr(i, "label", None),
            order_count=i.order_count,
            revenue=i.revenue,
            commission=i.commission,
            net_revenue=i.net_revenue,
            externally_settled_income=i.externally_settled_income,
            sponsorship_adjustment_id=i.sponsorship_adjustment_id,
            owner_expenses=i.owner_expenses,
            share_ratio_snapshot=i.share_ratio_snapshot,
            owner_net_amount=i.owner_net_amount,
            owner_net_amount_precise=item_precise_owner_net(i),
        )
        # 逐房行按房号排序在前，业主级支出行（room_id 空）排最后
        for i in sorted(settlement.items, key=lambda x: (x.room_id is None, x.room_id or ""))
    ]

    owner_amount_precise, actual_owner_amount_precise = settlement_precise_amounts(settlement.items)
    current = settlement_current_amounts(settlement)
    return SettlementDetailOut(
        settlement_id=settlement.settlement_id,
        owner_id=settlement.owner_id,
        billing_month=settlement.billing_month,
        total_net_revenue=current.total_net_revenue,
        owner_amount=current.owner_amount,
        deducted_expenses=current.deducted_expenses,
        actual_owner_amount=current.actual_owner_amount,
        owner_amount_precise=owner_amount_precise,
        actual_owner_amount_precise=actual_owner_amount_precise,
        status=settlement.status,
        notes=settlement.notes,
        created_at=settlement.created_at.isoformat() if settlement.created_at else "",
        items=items_out,
    )


@router.post("/{settlement_id}/confirm")
async def confirm_settlement(settlement_id: str, db: DBSession, current_user: CurrentUser):
    if current_user["role"] not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="无权确认结算")

    # Read only the lock scope first.  All financial snapshot reads follow the
    # global month -> owner -> settlement -> order lock order.
    billing_month = await db.scalar(
        select(OwnerSettlement.billing_month).where(
            OwnerSettlement.settlement_id == settlement_id
        )
    )
    if billing_month is None:
        raise HTTPException(status_code=404, detail="结算记录不存在")
    try:
        await acquire_month_financial_lock(db, billing_month)
    except ValueError:
        # The existing plain-language invalid-month response below remains the
        # public contract; malformed rows are serialized on a private key.
        await acquire_month_financial_lock(db, "1970-01")
    settlement = await _lock_settlement_with_owner(db, settlement_id)
    if not settlement:
        raise HTTPException(status_code=404, detail="结算记录不存在")
    if settlement.billing_month != billing_month:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "settlement_lock_scope_changed",
                "message": "结算月份已变化，请刷新后重试",
            },
        )
    if settlement.status != SettlementStatus.pending:
        raise HTTPException(status_code=400, detail="仅待确认状态可确认")

    try:
        year_text, month_text = settlement.billing_month.split("-", maxsplit=1)
        if (
            len(year_text) != 4
            or len(month_text) != 2
            or not year_text.isdigit()
            or not month_text.isdigit()
        ):
            raise ValueError
        year = int(year_text)
        month = int(month_text)
        if month < 1 or month > 12:
            raise ValueError
    except (AttributeError, ValueError):
        raise _service_fee_confirmation_error(
            settlement,
            unresolved=[
                {
                    "reason": "invalid_billing_month",
                    "order_id": None,
                    "room_id": None,
                    "stay_group_id": None,
                    "order_ids": [],
                    "room_ids": [],
                }
            ],
            action="请修正结算月份为 YYYY-MM，并重新生成结算后再确认。",
        )

    current_date = today_cn()
    if (year, month) >= (current_date.year, current_date.month):
        raise _service_fee_confirmation_error(
            settlement,
            unresolved=[
                {
                    "reason": "billing_month_not_closed",
                    "order_id": None,
                    "room_id": None,
                    "stay_group_id": None,
                    "order_ids": [],
                    "room_ids": [],
                }
            ],
            action="正式结算仅可确认已结束的自然月；请在月末后重新生成再确认。",
        )

    # 确认时重新体检，覆盖“生成后又导入账单/新增费用/改订单”的漂移窗口。
    preflight = await run_settlement_preflight(db, year, month)
    # Preserve the existing confirmation error contract for planner ambiguities.
    # The identical planner below still blocks these, with all related order IDs.
    planner_codes = {
        "service_fee_overlapping_stay_group", "service_fee_gapped_stay_group",
        "service_fee_duplicate_final_nodes", "service_fee_wrong_expense_month",
        "service_fee_duplicate_service_fee",
    }
    deferred_preflight_error = None
    if preflight.blocking:
        detail = {
            "code": "settlement_preflight_failed",
            "message": "月结体检发现未处理异常，请联系管理员处理并重新生成后再确认。",
        }
        # 全月报告可能包含其他业主的订单、房间和金额，只向后台角色披露。
        if current_user["role"] != "owner":
            detail["message"] = "月结体检发现未处理异常，请处理完成并重新生成后再确认。"
            detail["report"] = preflight.to_dict()
        preflight_error = HTTPException(
            status_code=409,
            detail=detail,
        )
        # A checkout moved after generation already has a precise snapshot-drift
        # response. Preserve it, but still reject wrongly posted fees below if
        # the stored financial snapshot happens to match.
        if all(issue.code in planner_codes for issue in preflight.issues) or all(
            issue.code == "service_fee_posted_wrong_month" for issue in preflight.issues
        ):
            deferred_preflight_error = preflight_error
        else:
            raise preflight_error

    # Confirmation is deliberately read-only: generation is the only path that
    # may repair missing fee rows.  The owner and settlement locks acquired above
    # keep the global owner -> settlement order while this final plan is read.
    fee_plan = await plan_service_fee_reconciliation(
        db, settlement.owner_id, year, month
    )
    if fee_plan.missing or fee_plan.unresolved:
        missing = [
            {
                "order_id": entry.order_id,
                "room_id": entry.room_id,
                "category": entry.category.value,
                "amount": f"{entry.amount:.2f}",
                "expense_date": entry.expense_date.isoformat(),
                "stay_group_id": entry.stay_group_id,
            }
            for entry in fee_plan.missing
        ]
        unresolved = [
            _unresolved_service_fee_detail(entry) for entry in fee_plan.unresolved
        ]
        raise _service_fee_confirmation_error(
            settlement,
            missing=missing,
            unresolved=unresolved,
            action=(
                "请先核查歧义订单，并作废错误费用或重新生成待确认结算以补齐缺失服务费，"
                "确认账本完整后再操作。"
            ),
        )

    stored_items = list(
        (
            await db.execute(
                select(OwnerSettlementItem).where(
                    OwnerSettlementItem.settlement_id == settlement.settlement_id
                )
            )
        ).scalars()
    )
    current_snapshot = await _build_settlement_financial_snapshot(
        db, settlement.owner_id, year, month
    )
    period_start = date(year, month, 1)
    period_end = (
        date(year + 1, 1, 1)
        if month == 12
        else date(year, month + 1, 1)
    )
    service_fees = list(
        await db.scalars(
            select(Expense).where(
                Expense.owner_id == settlement.owner_id,
                Expense.is_service_fee.is_(True),
                Expense.is_deleted.is_(False),
                Expense.expense_date >= period_start,
                Expense.expense_date < period_end,
            )
        )
    )
    service_fee_refs = {
        expense.expense_id: (
            f"{expense.order_id}:{expense.room_id}:{expense.category.value}"
        )
        for expense in service_fees
    }
    drift = _financial_snapshot_drift(
        settlement,
        stored_items,
        current_snapshot,
        service_fee_refs=service_fee_refs,
    )
    if drift is not None:
        raise HTTPException(
            status_code=409,
            detail={
                "code": "settlement_snapshot_drift",
                "message": "结算生成后的财务来源已变化，暂不能确认",
                "owner_id": settlement.owner_id,
                "billing_month": settlement.billing_month,
                "drift": drift,
                "action": (
                    "请核对订单归月、金额及房间/业主费用后，使用覆盖重新生成结算，"
                    "确认新快照无误后再操作。"
                ),
            },
        )
    if deferred_preflight_error is not None:
        raise deferred_preflight_error

    from app.api.v1.export import _settlement_income_rows
    import json
    await db.refresh(settlement, ['items'])
    income_rows, income_checks = await _settlement_income_rows(db, settlement, full_names=True, use_frozen=False)
    settlement.income_detail_snapshot = json.loads(json.dumps({
        'version': 1, 'rows': income_rows, 'checks': income_checks,
        'confirmed_by': current_user['user_id'], 'billing_month': settlement.billing_month,
        'sponsorship_adjustment_ids': [item.sponsorship_adjustment_id for item in settlement.items if item.sponsorship_adjustment_id],
    }, default=str, ensure_ascii=False))
    settlement.status = SettlementStatus.confirmed
    await db.commit()
    return {"message": "结算已确认"}


@router.post("/{settlement_id}/dispute")
async def dispute_settlement(settlement_id: str, body: DisputeBody, db: DBSession, current_user: CurrentUser):
    # 此前完全没有角色校验,任意已登录 token(含保洁)都能把任意结算标为争议并改备注 (#51)。
    # 与 confirm_settlement 对齐:仅 admin / owner 可争议。
    if current_user["role"] not in ("admin", "owner"):
        raise HTTPException(status_code=403, detail="无权操作结算")
    settlement = await _lock_settlement_with_owner(db, settlement_id)
    if not settlement:
        raise HTTPException(status_code=404, detail="结算记录不存在")

    settlement.status = SettlementStatus.disputed
    settlement.notes = body.notes
    await db.commit()
    return {"message": "已标记为有争议"}
