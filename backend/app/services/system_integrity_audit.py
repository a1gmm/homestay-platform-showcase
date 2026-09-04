"""Read-only cross-domain data integrity audit.

The audit deliberately loads only business identifiers and structural fields.
It never repairs data and never includes guest names, phone numbers, notes, or
credentials in its report, so the same service is safe for production checks.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import asdict, dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.settlement import OwnerSettlement


@dataclass(frozen=True)
class IntegrityIssue:
    code: str
    entity_ids: tuple[str, ...]
    message: str

    def to_dict(self) -> dict:
        data = asdict(self)
        data["entity_ids"] = list(self.entity_ids)
        return data


@dataclass(frozen=True)
class IntegrityReport:
    blocking: bool
    counts: dict[str, int]
    issues: list[IntegrityIssue]

    def to_dict(self) -> dict:
        return {
            "blocking": self.blocking,
            "counts": self.counts,
            "issues": [issue.to_dict() for issue in self.issues],
        }


async def run_system_integrity_audit(db: AsyncSession) -> IntegrityReport:
    """Inspect durable invariants using SELECT-only queries."""
    order_rows = (await db.execute(
        select(Order.order_id, Order.channel, Order.platform_order_id).where(
            Order.is_deleted.is_(False),
            Order.order_status != OrderStatus.cancelled,
        )
    )).all()
    valid_order_ids = {row.order_id for row in order_rows}

    room_rows = []
    if valid_order_ids:
        room_rows = (await db.execute(
            select(
                OrderRoom.order_room_id,
                OrderRoom.order_id,
                OrderRoom.room_id,
                OrderRoom.check_in_date,
                OrderRoom.check_out_date,
            ).where(OrderRoom.order_id.in_(valid_order_ids))
        )).all()

    issues: list[IntegrityIssue] = []

    for row in room_rows:
        if row.check_out_date <= row.check_in_date:
            issues.append(IntegrityIssue(
                code="invalid_stay_dates",
                entity_ids=(row.order_room_id, row.order_id),
                message="入住日期必须早于退房日期。",
            ))

    platform_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in order_rows:
        if row.platform_order_id:
            channel = row.channel.value if hasattr(row.channel, "value") else str(row.channel)
            platform_groups[(channel, row.platform_order_id)].append(row.order_id)
    for (_channel, platform_id), order_ids in platform_groups.items():
        unique_ids = tuple(sorted(set(order_ids)))
        if len(unique_ids) > 1:
            issues.append(IntegrityIssue(
                code="duplicate_active_platform_order",
                entity_ids=(platform_id, *unique_ids),
                message="同一渠道平台订单号关联多张有效订单。",
            ))

    settlement_rows = (await db.execute(
        select(
            OwnerSettlement.settlement_id,
            OwnerSettlement.owner_id,
            OwnerSettlement.billing_month,
        )
    )).all()
    settlement_groups: dict[tuple[str, str], list[str]] = defaultdict(list)
    for row in settlement_rows:
        settlement_groups[(row.owner_id, row.billing_month)].append(row.settlement_id)
    for (owner_id, month), settlement_ids in settlement_groups.items():
        unique_ids = tuple(sorted(set(settlement_ids)))
        if len(unique_ids) > 1:
            issues.append(IntegrityIssue(
                code="duplicate_owner_settlement",
                entity_ids=(owner_id, month, *unique_ids),
                message="同一业主同一月份存在多张结算单。",
            ))

    room_order_ids = {row.order_id for row in room_rows}
    for order_id in sorted(valid_order_ids - room_order_ids):
        issues.append(IntegrityIssue(
            code="order_without_room_row",
            entity_ids=(order_id,),
            message="有效订单缺少 order_rooms 明细。",
        ))

    stays_by_room: dict[str, list] = defaultdict(list)
    for row in room_rows:
        if row.room_id and row.check_out_date > row.check_in_date:
            stays_by_room[row.room_id].append(row)
    for room_id, stays in stays_by_room.items():
        ordered = sorted(stays, key=lambda row: (row.check_in_date, row.check_out_date, row.order_room_id))
        for index, first in enumerate(ordered):
            for second in ordered[index + 1:]:
                if second.check_in_date >= first.check_out_date:
                    break
                if first.order_id == second.order_id:
                    continue
                issues.append(IntegrityIssue(
                    code="overlapping_room_stays",
                    entity_ids=(room_id, first.order_room_id, second.order_room_id),
                    message="同一房间的两段有效入住日期发生重叠。",
                ))

    issues.sort(key=lambda issue: (issue.code, issue.entity_ids))
    counts = dict(sorted(Counter(issue.code for issue in issues).items()))
    return IntegrityReport(blocking=bool(issues), counts=counts, issues=issues)
