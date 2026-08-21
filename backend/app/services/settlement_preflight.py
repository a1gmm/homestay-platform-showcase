"""月结前完整性体检。

本服务只读数据库，不修数。生成和确认结算都复用同一份报告，任何 blocking
异常都必须先由运营处理，避免月底靠逐单肉眼复核。
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import date
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expense import Expense, ExpensePayer
from app.models.order import (
    Order,
    OrderStatus,
    OTA_PLATFORM_CHANNELS,
    is_owner_self_order,
)
from app.models.order_room import OrderRoom
from app.models.recon import ReconBatch, ReconDiff, ReconDiffStatus
from app.services.order_pricing import (
    effective_owner_revenue_for_room,
    order_room_commission,
    safe_decimal,
)


CENT = Decimal("0.01")
OPEN_RECON_STATUSES = frozenset({
    ReconDiffStatus.pending,
    ReconDiffStatus.appeal_pending,
})


@dataclass(frozen=True)
class SettlementPreflightIssue:
    code: str
    message: str
    order_id: str | None = None
    room_id: str | None = None
    expense_id: str | None = None
    recon_diff_id: str | None = None
    platform_order_id: str | None = None
    amount: Decimal | None = None

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        # HTTPException.detail 不经过 FastAPI response_model 的 Decimal 编码，必须在服务
        # 边界转成字符串；金额也因此保持两位小数，不被 JSON float 磨损。
        data["amount"] = str(self.amount) if self.amount is not None else None
        return data


@dataclass(frozen=True)
class SettlementPreflightReport:
    billing_month: str
    blocking: bool
    counts: dict[str, int]
    issues: list[SettlementPreflightIssue]

    def to_dict(self) -> dict[str, Any]:
        return {
            "billing_month": self.billing_month,
            "blocking": self.blocking,
            "counts": self.counts,
            "issues": [issue.to_dict() for issue in self.issues],
        }


def _month_window(year: int, month: int) -> tuple[date, date]:
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    return start, end


async def _pricing_issues(
    db: AsyncSession, start: date, end: date
) -> list[SettlementPreflightIssue]:
    rows = (await db.execute(
        select(Order, OrderRoom)
        .join(OrderRoom, OrderRoom.order_id == Order.order_id)
        .where(
            Order.is_deleted.is_(False),
            Order.order_status != OrderStatus.cancelled,
            OrderRoom.check_out_date >= start,
            OrderRoom.check_out_date < end,
        )
    )).all()
    if not rows:
        return []

    order_ids = {order.order_id for order, _ in rows}
    room_counts = Counter((await db.execute(
        select(OrderRoom.order_id).where(OrderRoom.order_id.in_(order_ids))
    )).scalars().all())

    totals: dict[str, Decimal] = defaultdict(lambda: Decimal("0"))
    first_room: dict[str, str | None] = {}
    orders: dict[str, Order] = {}
    for order, order_room in rows:
        if order.channel in OTA_PLATFORM_CHANNELS:
            continue
        effective_net = effective_owner_revenue_for_room(
            order_room.ota_owner_revenue,
            order_room.actual_price,
            order.metadata_,
            room_counts[order.order_id],
        )
        commission = order_room_commission(
            order_room.actual_price,
            effective_net,
            order.platform_commission_rate,
        )
        totals[order.order_id] += commission
        first_room.setdefault(order.order_id, order_room.room_id)
        orders[order.order_id] = order

    issues: list[SettlementPreflightIssue] = []
    for order_id, commission in totals.items():
        order = orders[order_id]
        subsidy = safe_decimal((order.metadata_ or {}).get("ota_subsidy"))
        if commission == 0 and subsidy == 0:
            continue
        amount = (abs(commission) + abs(subsidy)).quantize(CENT)
        issues.append(SettlementPreflightIssue(
            code="nonplatform_commission",
            message="线下/自来客订单仍带平台佣金或补贴，请先清理平台定价字段。",
            order_id=order_id,
            room_id=first_room[order_id],
            platform_order_id=order.platform_order_id,
            amount=amount,
        ))
    return issues


async def _expense_issues(
    db: AsyncSession, start: date, end: date
) -> list[SettlementPreflightIssue]:
    rows = (await db.execute(
        select(Expense, Order)
        .outerjoin(Order, Order.order_id == Expense.order_id)
        .where(
            Expense.is_deleted.is_(False),
            Expense.payer == ExpensePayer.owner,
            Expense.order_id.is_not(None),
            Expense.expense_date >= start,
            Expense.expense_date < end,
        )
    )).all()

    issues: list[SettlementPreflightIssue] = []
    for expense, order in rows:
        amount = Decimal(expense.amount or 0).quantize(CENT)
        if order is None or order.is_deleted or order.order_status == OrderStatus.cancelled:
            issues.append(SettlementPreflightIssue(
                code="invalid_order_expense",
                message="业主费用关联的订单已取消、删除或不存在，请先作废或改由公司承担。",
                order_id=expense.order_id,
                room_id=expense.room_id,
                expense_id=expense.expense_id,
                amount=amount,
            ))
            continue
        if is_owner_self_order(order):
            issues.append(SettlementPreflightIssue(
                code="owner_self_owner_expense",
                message="业主自住费用应由公司承担，请先把费用承担方改为公司。",
                order_id=expense.order_id,
                room_id=expense.room_id,
                expense_id=expense.expense_id,
                amount=amount,
            ))
    return issues


async def _reconciliation_issues(
    db: AsyncSession, billing_month: str
) -> list[SettlementPreflightIssue]:
    rows = (await db.execute(
        select(ReconDiff)
        .join(ReconBatch, ReconBatch.batch_id == ReconDiff.batch_id)
        .where(
            ReconBatch.bill_month == billing_month,
            ReconDiff.status.in_(OPEN_RECON_STATUSES),
        )
    )).scalars().all()
    return [
        SettlementPreflightIssue(
            code="open_reconciliation",
            message="平台账单仍有未处理差异，请先完成账单对账。",
            order_id=row.order_id,
            recon_diff_id=row.diff_id,
            platform_order_id=row.platform_order_id,
            amount=(Decimal(row.bill_amount).quantize(CENT) if row.bill_amount is not None else None),
        )
        for row in rows
    ]


async def _duplicate_platform_issues(
    db: AsyncSession, start: date, end: date
) -> list[SettlementPreflightIssue]:
    rows = (await db.execute(
        select(Order.channel, Order.platform_order_id, Order.order_id)
        .join(OrderRoom, OrderRoom.order_id == Order.order_id)
        .where(
            Order.is_deleted.is_(False),
            Order.order_status != OrderStatus.cancelled,
            Order.platform_order_id.is_not(None),
            OrderRoom.check_out_date >= start,
            OrderRoom.check_out_date < end,
        )
        .distinct()
    )).all()
    grouped: dict[tuple[str, str], set[str]] = defaultdict(set)
    for channel, platform_order_id, order_id in rows:
        if platform_order_id:
            grouped[(channel.value, platform_order_id)].add(order_id)

    issues: list[SettlementPreflightIssue] = []
    for (_channel, platform_order_id), order_ids in grouped.items():
        if len(order_ids) < 2:
            continue
        ordered = sorted(order_ids)
        issues.append(SettlementPreflightIssue(
            code="duplicate_platform_order",
            message=f"同一平台订单号关联了多张有效订单：{', '.join(ordered)}。",
            order_id=ordered[0],
            platform_order_id=platform_order_id,
        ))
    return issues


async def run_settlement_preflight(
    db: AsyncSession, year: int, month: int
) -> SettlementPreflightReport:
    """返回指定自然月的只读阻断报告。"""
    start, end = _month_window(year, month)
    billing_month = f"{year:04d}-{month:02d}"
    issues = [
        *await _pricing_issues(db, start, end),
        *await _expense_issues(db, start, end),
        *await _reconciliation_issues(db, billing_month),
        *await _duplicate_platform_issues(db, start, end),
    ]
    issues.sort(key=lambda issue: (
        issue.code,
        issue.order_id or "",
        issue.expense_id or "",
        issue.recon_diff_id or "",
    ))
    counts = dict(sorted(Counter(issue.code for issue in issues).items()))
    return SettlementPreflightReport(
        billing_month=billing_month,
        blocking=bool(issues),
        counts=counts,
        issues=issues,
    )
