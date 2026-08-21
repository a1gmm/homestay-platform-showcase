from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import delete, func, select
from sqlalchemy.orm import selectinload
from typing import Optional
from decimal import Decimal
from datetime import datetime
from dataclasses import dataclass
import json
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
from app.models.expense import ExpenseCategory, EXPENSE_CATEGORY_LABELS
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

router = APIRouter(prefix="/settlements", tags=["settlements"])


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


def _item_fingerprint(item) -> tuple:
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
            item.cost_share_breakdown or [],
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
    stored_fingerprint = sorted(_item_fingerprint(item) for item in stored_items)
    current_fingerprint = sorted(_item_fingerprint(item) for item in current.items)
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


async def _generate_settlements_core(
    db, year: int, month: int, created_by: Optional[str] = None,
    overwrite: bool = False,
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

        settlement_id = "STL-" + uuid.uuid4().hex[:12].upper()
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
        sponsorship_bindings: list[tuple[CompanySponsoredStay, OwnerSettlementItem]] = []
        sponsorship_income_by_room = await load_room_sponsorship_income(
            db, [room.room_id for room in rooms], year, month, for_update=True
        )
        for item in snapshot.items:
            settlement_item = OwnerSettlementItem(
                item_id="SLI-" + uuid.uuid4().hex[:12].upper(),
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
                cost_share_breakdown=item.cost_share_breakdown,
            )
            db.add(settlement_item)
            if item.room_id is not None:
                for income in sponsorship_income_by_room.get(item.room_id, []):
                    sponsorship_bindings.append((income.root, settlement_item))
        # Root consistency validation reads the referenced item from the database.
        # Flush the new batch/items first, still inside this single uncommitted
        # transaction, then attach and settle every included sponsorship root.
        await db.flush()
        for root, item in sponsorship_bindings:
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
    await db.commit()
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

    result = await _generate_settlements_core(
        db, year, month, current_user["user_id"], overwrite=overwrite)
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

    settlement = await _lock_settlement_with_owner(db, settlement_id)
    if not settlement:
        raise HTTPException(status_code=404, detail="结算记录不存在")
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
    if preflight.blocking:
        detail = {
            "code": "settlement_preflight_failed",
            "message": "月结体检发现未处理异常，请联系管理员处理并重新生成后再确认。",
        }
        # 全月报告可能包含其他业主的订单、房间和金额，只向后台角色披露。
        if current_user["role"] != "owner":
            detail["message"] = "月结体检发现未处理异常，请处理完成并重新生成后再确认。"
            detail["report"] = preflight.to_dict()
        raise HTTPException(
            status_code=409,
            detail=detail,
        )

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
    drift = _financial_snapshot_drift(
        settlement, stored_items, current_snapshot
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
