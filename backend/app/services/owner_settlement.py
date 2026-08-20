"""单房单月业主分成计算——结算单生成与业主门户月度统计的共享口径。

历史问题（2026-07-08 架构扫描收敛）：settlements.py 与 owner_portal.py 各算一套，
公式版本（v1/v2）、费用加权（RoomCostShareRule）、平台补贴三处口径分叉，
业主门户看到的数字和结算单对不上。本模块是唯一实现，两端只准调这里。

口径（均为王总已拍板的 v2 结算口径）：
- 月度归属按「离店日期」(check_out_date) 算（2026-07-14 拍板）：跨月订单整笔归
  离店月，例 6/30 入住 7/1 离店 → 7 月。支出侧仍按 expense_date（发生日）归月。
- 佣金逐 OrderRoom 算（order_room_commission）：手填每房净房费的行按
  房费−净值，否则按房费×整单佣金率。
- 平台补贴 metadata.ota_subsidy 计入净收入（2026-06-30 裁决）。
- 业主费用按 RoomCostShareRule (booking_type × category) 的 share_percent
  加权；无规则 fallback 到 payer=owner / owner_deduction_rules 全担。
- v2 公式：业主净得 = 净收入 × 分成比例 − 已加权业主费用。
  （v1 的 (净收入−费用)×比例 在 share_percent≠1 时错算，禁止回退。）
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal
from typing import Optional

from sqlalchemy import select, extract, func, or_, not_
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import raiseload

from app.models.expense import Expense, ExpenseCategory, ExpensePayer
from app.models.company_sponsored_stay import (
    CompanySponsoredStay,
    CompanySponsorshipStatus,
    PaymentResponsibility,
)
from app.models.company_sponsorship_adjustment import CompanySponsorshipAdjustment
from app.models.order import BookingType, Channel, Order, OrderStatus, StaySettlementKind
from app.models.order_room import OrderRoom
from app.models.room import Room
from app.models.room_cost_share import RoomCostShareRule
from app.services.order_pricing import (
    effective_owner_revenue_for_room,
    order_room_commission,
    safe_decimal,
)


@dataclass
class RoomMonthOwnerStat:
    room_id: str
    order_count: int = 0
    nights: int = 0
    revenue: Decimal = Decimal("0")
    commission: Decimal = Decimal("0")
    subsidy: Decimal = Decimal("0")
    # 公司承担事实中的 company_payable 原始金额；channel_settled 仍属于业主收入，
    # 但不进入公司对业主的实际应付。
    company_payable: Decimal = Decimal("0")
    externally_settled_income: Decimal = Decimal("0")
    net_revenue: Decimal = Decimal("0")
    owner_expenses: Decimal = Decimal("0")
    # 业主分成（扣业主费用之前）= net_revenue × share_ratio
    owner_revenue_share: Decimal = Decimal("0")
    # 业主实付 = owner_revenue_share − owner_expenses（v2 公式）
    owner_net: Decimal = Decimal("0")
    share_ratio: Decimal = Decimal("0")
    cost_share_breakdown: list = field(default_factory=list)


@dataclass(frozen=True)
class CompanySponsoredIncome:
    sponsorship_id: str
    order_id: str
    amount: Decimal
    payment_responsibility: PaymentResponsibility
    root: CompanySponsoredStay = field(repr=False, compare=False)


@dataclass(frozen=True)
class CompanySponsoredPayable:
    sponsorship_id: str
    order_id: str
    amount: Decimal


@dataclass(frozen=True)
class SettlementCurrentAmounts:
    total_net_revenue: Decimal
    owner_amount: Decimal
    deducted_expenses: Decimal
    actual_owner_amount: Decimal


def settlement_current_amounts(settlement) -> SettlementCurrentAmounts:
    """Apply append-only correction lines without mutating the original header."""
    correction_lines = [
        item
        for item in settlement.items
        if getattr(item, "sponsorship_adjustment_id", None) is not None
    ]
    net_delta = sum(
        (Decimal(item.net_revenue) for item in correction_lines), Decimal("0.00")
    )
    owner_delta = sum(
        (
            (Decimal(item.net_revenue) * Decimal(item.share_ratio_snapshot)).quantize(
                Decimal("0.01")
            )
            for item in correction_lines
        ),
        Decimal("0.00"),
    )
    payable_delta = sum(
        (Decimal(item.owner_net_amount) for item in correction_lines), Decimal("0.00")
    )
    return SettlementCurrentAmounts(
        total_net_revenue=Decimal(settlement.total_net_revenue) + net_delta,
        owner_amount=Decimal(settlement.owner_amount) + owner_delta,
        deducted_expenses=Decimal(settlement.deducted_expenses),
        actual_owner_amount=Decimal(settlement.actual_owner_amount) + payable_delta,
    )


async def load_room_sponsorship_income(
    db: AsyncSession,
    room_ids: list[str],
    year: int,
    month: int,
    cutoff: date | None = None,
    for_update: bool = False,
    start_date: date | None = None,
    end_date: date | None = None,
) -> dict[str, list[CompanySponsoredIncome]]:
    """Batch-load effective sponsorship income for a room set in one query.

    Adjustment rows are aggregated in SQL and never reached through ORM
    relationships, so callers can load this once before their room loop.
    """
    if not room_ids:
        return {}
    adjustment_totals = (
        select(
            CompanySponsorshipAdjustment.sponsorship_id.label("sponsorship_id"),
            func.sum(CompanySponsorshipAdjustment.delta).label("adjustment_total"),
        )
        .group_by(CompanySponsorshipAdjustment.sponsorship_id)
        .subquery()
    )
    effective_amount = (
        CompanySponsoredStay.amount
        + func.coalesce(adjustment_totals.c.adjustment_total, Decimal("0.00"))
    ).label("effective_amount")
    statement = (
        select(
            OrderRoom.room_id,
            CompanySponsoredStay,
            effective_amount,
        )
        .options(raiseload(CompanySponsoredStay.adjustments))
        .join(
            CompanySponsoredStay,
            CompanySponsoredStay.segment_order_id == OrderRoom.order_id,
        )
        .join(Order, Order.order_id == OrderRoom.order_id)
        .outerjoin(
            adjustment_totals,
            adjustment_totals.c.sponsorship_id
            == CompanySponsoredStay.sponsored_stay_id,
        )
        .where(OrderRoom.room_id.in_(room_ids))
        .where(Order.is_deleted == False)
        .where(Order.order_status != OrderStatus.cancelled)
        .where(not_(or_(
            Order.booking_type == BookingType.owner_self,
            Order.channel == Channel.self_used,
        )))
        .where(CompanySponsoredStay.status != CompanySponsorshipStatus.voided)
        .order_by(
            OrderRoom.room_id,
            CompanySponsoredStay.segment_check_out_date,
            CompanySponsoredStay.sponsored_stay_id,
        )
    )
    if start_date is not None and end_date is not None:
        statement = statement.where(
            OrderRoom.check_out_date >= start_date,
            OrderRoom.check_out_date <= end_date,
        )
    else:
        statement = statement.where(
            extract("year", OrderRoom.check_out_date) == year,
            extract("month", OrderRoom.check_out_date) == month,
        )
    if cutoff is not None:
        statement = statement.where(OrderRoom.check_out_date <= cutoff)
    if for_update:
        statement = statement.with_for_update(of=CompanySponsoredStay)

    by_room: dict[str, list[CompanySponsoredIncome]] = {
        room_id: [] for room_id in room_ids
    }
    for room_id, root, amount in (
        await db.execute(statement)
    ).all():
        by_room.setdefault(room_id, []).append(
            CompanySponsoredIncome(
                sponsorship_id=root.sponsored_stay_id,
                order_id=root.segment_order_id,
                amount=Decimal(amount).quantize(Decimal("0.01")),
                payment_responsibility=root.payment_responsibility,
                root=root,
            )
        )
    return by_room


async def build_company_sponsored_payable(
    db: AsyncSession, sponsorship: CompanySponsoredStay
) -> CompanySponsoredPayable | None:
    """Return the current raw company obligation, excluding external settlement."""
    if (
        sponsorship.status == CompanySponsorshipStatus.voided
        or sponsorship.payment_responsibility != PaymentResponsibility.company_payable
    ):
        return None
    adjustment_total = await db.scalar(
        select(func.coalesce(func.sum(CompanySponsorshipAdjustment.delta), 0)).where(
            CompanySponsorshipAdjustment.sponsorship_id
            == sponsorship.sponsored_stay_id
        )
    )
    return CompanySponsoredPayable(
        sponsorship_id=sponsorship.sponsored_stay_id,
        order_id=sponsorship.segment_order_id,
        amount=(
            Decimal(sponsorship.amount) + Decimal(adjustment_total or 0)
        ).quantize(Decimal("0.01")),
    )


# ── 账面「到厘」显示（王总 2026-07-19）───────────────────────────────────
# 结算页/业主端把「业主分成·业主应得」显示到小数第三位，让人看到真实分成值；
# 但**打款金额不变**——存库的 owner_amount/actual_owner_amount 仍是到分的值，
# 实际转账按分四舍五入（人民币最小单位是分）。这里只从**已存明细**重算展示值，
# 不落库、不改打款、不需重跑历史。
#
# 口径：逐房 net_revenue × share_ratio 量化到 0.001（与存库到分同一乘式，只是
# 少舍一位），累加为分成；再减去各行业主承担支出（本就是到分的真钱）= 应得。
# 业主级支出行 room_id 为空、net_revenue=0，天然只贡献支出、不贡献分成。
_MILLE = Decimal("0.001")


def item_precise_owner_net(item) -> Decimal:
    """单条明细的「业主应得·到厘」= net_revenue×share_ratio(到厘) − 业主承担支出。"""
    payable_base = Decimal(item.net_revenue) - Decimal(
        getattr(item, "externally_settled_income", 0) or 0
    )
    share = (payable_base * Decimal(item.share_ratio_snapshot)).quantize(_MILLE)
    return share - Decimal(item.owner_expenses)


def settlement_precise_amounts(items) -> tuple[Optional[Decimal], Optional[Decimal]]:
    """整单的（业主分成·到厘, 业主应得·到厘）。items 为结算明细行（含
    net_revenue / share_ratio_snapshot / owner_expenses 三字段即可）。

    无明细（旧版结算，前端 emptyText「未生成按房号的明细」）返回 (None, None)
    —— 绝不能返回 (0, 0)，否则前端把 0 当有效到厘值，把真实到分打款额（如
    ¥5244.69）盖成 ¥0.000。返回 None 让前端回退显示存库的到分金额。"""
    if not items:
        return None, None
    owner_amount = Decimal("0")
    total_expenses = Decimal("0")
    for it in items:
        ratio = Decimal(it.share_ratio_snapshot)
        owner_amount += (Decimal(it.net_revenue) * ratio).quantize(_MILLE)
        externally_settled_share = (
            Decimal(getattr(it, "externally_settled_income", 0) or 0) * ratio
        ).quantize(_MILLE)
        total_expenses += Decimal(it.owner_expenses)
        total_expenses += externally_settled_share
    return owner_amount, owner_amount - total_expenses


async def compute_room_period_owner_stat(
    db: AsyncSession, room: Room, start_date: date, end_date: date,
    cutoff: date | None = None,
    sponsorship_income_by_room: dict[str, list[CompanySponsoredIncome]] | None = None,
) -> RoomMonthOwnerStat:
    """算一间房某日期区间的业主分成明细。room 需已加载 owner_share_ratio /
    owner_deduction_rules / owner_ignored_categories。

    cutoff：可选「结算截止日」。传入时只统计**离店日 ≤ cutoff** 的订单
    （业主门户「结算到昨天」口径，2026-07-14 王总拍板：本月未退房的收入
    不提前算给业主，退房后再计入）。**结算单生成不传 cutoff = 整月**，
    确保月底真实分账口径不受影响。费用侧不受 cutoff 影响（按发生日归月）。"""
    stat = RoomMonthOwnerStat(room_id=room.room_id)

    # ── 收入侧：按 OrderRoom 粒度逐行算佣金（每房佣金口径 2026-07-04 起分两档，
    #    SQL 聚合表达不动了）。多房订单跨 owner 时每个 owner 只拿自己那行。
    or_stmt = (
        select(
            OrderRoom,
            Order.platform_commission_rate,
            Order.metadata_,
            Order.order_id,
            Order.stay_settlement_kind,
        )
        .join(Order, Order.order_id == OrderRoom.order_id)
        .where(OrderRoom.room_id == room.room_id)
        .where(Order.is_deleted == False)
        .where(Order.order_status != OrderStatus.cancelled)
        .where(not_(or_(
            Order.booking_type == BookingType.owner_self,
            Order.channel == Channel.self_used,
        )))
        .where(OrderRoom.check_out_date >= start_date)
        .where(OrderRoom.check_out_date <= end_date)
    )
    if cutoff is not None:
        # 「结算到昨天」：业主门户只算已退房走人的单（离店日 ≤ cutoff）；
        # 不传 cutoff 的结算单生成路径整月全算，月底真实分账口径不受影响。
        or_stmt = or_stmt.where(OrderRoom.check_out_date <= cutoff)
    or_rows = (await db.execute(or_stmt)).all()
    order_ids: set = {order_id_ for _, _, _, order_id_, _ in or_rows}
    # 每单总房间数：判定「单房单」——单房单可直接用订单级冻结的平台原始到手结算
    #（绕开佣金率四舍五入磨损，王总 2026-07-14）。多房单订单级到手横跨多间，不接管。
    room_counts: dict = {}
    if order_ids:
        room_counts = dict((await db.execute(
            select(OrderRoom.order_id, func.count())
            .where(OrderRoom.order_id.in_(order_ids))
            .group_by(OrderRoom.order_id)
        )).all())
    platform_subsidy = Decimal("0")
    for or_row, comm_rate, order_meta, order_id_, settlement_kind in or_rows:
        is_company_sponsored = (
            settlement_kind == StaySettlementKind.company_sponsored
        )
        per_room_net = None
        if not is_company_sponsored:
            stat.revenue += Decimal(or_row.actual_price or 0)
            per_room_net = effective_owner_revenue_for_room(
                or_row.ota_owner_revenue,
                or_row.actual_price,
                order_meta,
                room_counts.get(order_id_),
            )
            stat.commission += order_room_commission(
                or_row.actual_price, per_room_net, comm_rate
            )
        # 平台补贴(OTA 倒贴: 业主到手>客人房费, 佣金率不能为负无法表达)。
        # 只写在**单间**单的 metadata.ota_subsidy, 故每单仅一行 OrderRoom, 不重复计;
        # 多房单不写该字段(走告警人工)。计入业主收入(王总裁决 2026-06-30)。
        # ⚠️ 只在 per_room_net is None(没用到手价当锚, 即到手>房费的倒贴单)时才加:
        # 到手价 ≤ 房费时业主净收入已直接锚定到手价, ota_subsidy 已含其中, 再加=双算
        # (对账把房费抬到>到手后会翻档踩到, 2026-07-15 王总实证)。定时对账会持续回写
        # ota_subsidy, 故只能在此消费端做条件, 不能靠删数据。脏值安全解析不炸整批结算。
        if not is_company_sponsored and per_room_net is None:
            platform_subsidy += safe_decimal((order_meta or {}).get("ota_subsidy"))
        stat.nights += max((or_row.check_out_date - or_row.check_in_date).days, 0)
    stat.order_count = len(order_ids)
    if sponsorship_income_by_room is None:
        sponsorship_income_by_room = await load_room_sponsorship_income(
            db,
            [room.room_id],
            start_date.year,
            start_date.month,
            cutoff=cutoff,
            start_date=start_date,
            end_date=end_date,
        )
    sponsorship_income = sponsorship_income_by_room.get(room.room_id, [])
    sponsored_total = sum(
        (entry.amount for entry in sponsorship_income), Decimal("0.00")
    )
    stat.company_payable = sum(
        (
            entry.amount
            for entry in sponsorship_income
            if entry.payment_responsibility == PaymentResponsibility.company_payable
        ),
        Decimal("0.00"),
    )
    stat.externally_settled_income = sponsored_total - stat.company_payable
    stat.subsidy = platform_subsidy + sponsored_total
    stat.net_revenue = stat.revenue - stat.commission + stat.subsidy

    # ── 费用侧（v2-issue#4）：优先 RoomCostShareRule 二维配置（占比%）；
    #    没有规则时 fallback 到老的 owner_deduction_rules + share_percent=1.0。
    deduction_rules = room.owner_deduction_rules or []
    ignored_categories = getattr(room, "owner_ignored_categories", None) or []

    exp_rows = (
        await db.execute(
            select(Expense, Order.booking_type)
            .outerjoin(Order, Order.order_id == Expense.order_id)
            .where(Expense.room_id == room.room_id)
            .where(Expense.is_deleted == False)
            .where(or_(
                Expense.order_id.is_(None),
                not_(or_(
                    Order.booking_type == BookingType.owner_self,
                    Order.channel == Channel.self_used,
                )),
            ))
            .where(Expense.expense_date >= start_date)
            .where(Expense.expense_date <= end_date)
        )
    ).all()

    rule_rows = (
        await db.execute(
            select(RoomCostShareRule).where(RoomCostShareRule.room_id == room.room_id)
        )
    ).scalars().all()
    rule_lookup: dict = {(r.booking_type, r.expense_category): r for r in rule_rows}

    for expense, booking_type in exp_rows:
        # 手工保洁明确标记为公司承担时，公司就是最终承担方；不得再被保洁 100%
        # 的硬规则或 RoomCostShareRule 重新分配给业主。系统自动服务费
        # (is_service_fee=True) 仍按既有规则由业主承担。
        if (
            expense.category == ExpenseCategory.cleaning
            and expense.payer == ExpensePayer.company
            and not getattr(expense, "is_service_fee", False)
        ):
            continue
        # 房东全担的硬规则服务费：历史 cleaning（无标记）按类目认定；洗涤/日耗等
        # 系统自动生成的服务费按 is_service_fee 标记认定。二者都强制 100%、不受占比/
        # 忽略类目影响。手工录的洗涤/日耗（is_service_fee=False）仍走占比规则。
        is_forced_owner_fee = (
            expense.category == ExpenseCategory.cleaning
            or getattr(expense, "is_service_fee", False)
        )
        # 强制全担费**不受 owner_ignored_categories 抑制**——否则某房把 cleaning/日耗
        # 列进忽略类目会静默漏扣（评审 Finding 3）。别的类目照常可忽略。
        if not is_forced_owner_fee and expense.category in ignored_categories:
            continue
        bt = booking_type or BookingType.normal  # 没关联订单的费用视为 normal
        rule = rule_lookup.get((bt, expense.category))
        if is_forced_owner_fee:
            # 房东全担 100%（王总 2026-07-12/07-14 拍板）。固定单价记账，该金额即业主
            # 应担全额，绝不再被 RoomCostShareRule 占比二次打折——否则会静默少扣（评审
            # 5.1 头号地雷）。故恒定 1.0，明细不挂 rule_id 避免误导。
            share_percent = Decimal("1.000")
            rule = None
        elif rule is not None:
            share_percent = Decimal(str(rule.share_percent))
        else:
            # Fallback to v1 owner_deduction_rules / payer=owner Y-N 逻辑
            if (
                expense.payer == ExpensePayer.owner
                or expense.category.value in deduction_rules
                or expense.category in deduction_rules
            ):
                share_percent = Decimal("1.000")
            else:
                share_percent = Decimal("0.000")
        owner_amount = (Decimal(str(expense.amount)) * share_percent).quantize(
            Decimal("0.01")
        )
        stat.owner_expenses += owner_amount
        stat.cost_share_breakdown.append(
            {
                "expense_id": expense.expense_id,
                "category": expense.category.value,
                "booking_type": (bt.value if hasattr(bt, "value") else str(bt)),
                "amount": str(Decimal(str(expense.amount)).quantize(Decimal("0.01"))),
                "share_percent": str(share_percent),
                "owner_amount": str(owner_amount),
                "rule_id": rule.rule_id if rule else None,
            }
        )

    # 平台补贴计入 net_revenue, 在明细里留一条可审计记录(非业主支出, 是收入加项)
    if platform_subsidy > 0:
        stat.cost_share_breakdown.append({
            "type": "platform_subsidy",
            "category": "平台补贴",
            "amount": str(platform_subsidy.quantize(Decimal("0.01"))),
            "note": "OTA 平台倒贴(业主到手>客人房费), 计入业主收入",
        })
    for entry in sponsorship_income:
        externally_settled = (
            entry.payment_responsibility == PaymentResponsibility.channel_settled
        )
        stat.cost_share_breakdown.append(
            {
                "source": "company_sponsored_stay",
                "type": "company_sponsored_stay",
                "sponsorship_id": entry.sponsorship_id,
                "order_id": entry.order_id,
                "amount": str(entry.amount.quantize(Decimal("0.01"))),
                "payment_responsibility": entry.payment_responsibility.value,
                "externally_settled": externally_settled,
            }
        )

    stat.share_ratio = Decimal(str(room.owner_share_ratio or 0))
    stat.owner_revenue_share = (stat.net_revenue * stat.share_ratio).quantize(Decimal("0.01"))
    # v2 公式 — owner_expenses 已是"按 share_percent 加权后业主承担的金额"，
    # 不再二次乘 share_ratio。语义：业主分成 - 业主承担费用 = 实际打款。
    payable_owner_share = (
        (stat.net_revenue - stat.externally_settled_income) * stat.share_ratio
    ).quantize(Decimal("0.01"))
    stat.owner_net = (payable_owner_share - stat.owner_expenses).quantize(Decimal("0.01"))
    return stat


async def compute_room_month_owner_stat(
    db: AsyncSession, room: Room, year: int, month: int,
    cutoff: date | None = None,
    sponsorship_income_by_room: dict[str, list[CompanySponsoredIncome]] | None = None,
) -> RoomMonthOwnerStat:
    """月度包装器；结算、业主门户与任意区间财务核对共用同一计算实现。"""
    from calendar import monthrange

    return await compute_room_period_owner_stat(
        db,
        room,
        date(year, month, 1),
        date(year, month, monthrange(year, month)[1]),
        cutoff=cutoff,
        sponsorship_income_by_room=sponsorship_income_by_room,
    )


@dataclass
class OwnerLevelExpenses:
    """业主级支出（room_id 为空、直接挂 owner_id）汇总——不属于任何单间。"""
    total_owner_borne: Decimal = Decimal("0")   # 业主承担合计（整笔 100%）
    breakdown: list = field(default_factory=list)  # 展示明细：[{expense_id, category, amount, description}]


async def compute_owner_level_expenses(
    db: AsyncSession, owner_id: str, year: int, month: int, cutoff: date | None = None
) -> OwnerLevelExpenses:
    """业主级支出（整层/某业主录入，room_id 为空）中由业主承担的部分。

    方案 B（王总 2026-07-18）：一条不拆、整笔 100% 计入业主承担，不套 RoomCostShareRule
    逐房占比（无房可套）。仅 payer=owner 的才从业主分成扣；payer=company 是公司成本，
    不在此扣减。支出按 expense_date（发生日）归月；cutoff 有值时只算到该日（业主端结算到昨天）。
    """
    q = (
        select(Expense)
        .where(
            Expense.owner_id == owner_id,
            Expense.room_id.is_(None),
            Expense.is_deleted == False,
            Expense.payer == ExpensePayer.owner,
            extract("year", Expense.expense_date) == year,
            extract("month", Expense.expense_date) == month,
        )
    )
    if cutoff is not None:
        q = q.where(Expense.expense_date <= cutoff)

    out = OwnerLevelExpenses()
    for e in (await db.execute(q)).scalars().all():
        amt = Decimal(str(e.amount)).quantize(Decimal("0.01"))
        out.total_owner_borne += amt
        out.breakdown.append({
            "expense_id": e.expense_id,
            "category": e.category.value,
            "amount": str(amt),
            "share_percent": "1.0000",
            "owner_amount": str(amt),
            "description": e.description,
        })
    out.total_owner_borne = out.total_owner_borne.quantize(Decimal("0.01"))
    return out
