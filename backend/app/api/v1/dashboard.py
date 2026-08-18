import asyncio
import calendar
from datetime import date, datetime, timezone, timedelta
from decimal import Decimal

from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, func, extract, case
from dateutil.relativedelta import relativedelta

from app.core.deps import DBSession, CurrentUser
from app.core.database import AsyncSessionLocal
from app.core.datetime_helpers import today_cn, CN_TZ
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.expense import Expense
from app.models.task import Task, TaskStatus
from app.models.room import Room
from app.services.dashboard_metrics import compute_period_metrics

router = APIRouter(prefix="/dashboard", tags=["dashboard"])


# ─── Internal SQL-aggregation helpers (shared by batch + legacy endpoints) ───

async def _today_stats(db) -> dict:
    today = today_cn()
    now_utc = datetime.now(timezone.utc)

    # 「今日待入住」/「今日待退房」按 check_in_date / check_out_date == today 计数，
    # 状态过滤包含所有「中间态」——确认订单 / 排房后仍属待入住；发起退房 / 确认收款后仍属待退房。
    # cancelled / completed / abnormal / rescheduled 不计。
    # 2026-06-05 流程调整：pending_payment 已重定义为后期「待完成」态，移出待入住、并入待退房。
    pre_checkin_statuses = (
        OrderStatus.pending_confirm,
        OrderStatus.paid_pending_room,
        OrderStatus.roomed_pending_checkin,
    )
    pre_checkout_statuses = (
        OrderStatus.checked_in,
        OrderStatus.pending_checkout,
        OrderStatus.pending_payment,
    )

    # Single aggregate query counting multiple conditions via CASE
    row = (await db.execute(
        select(
            func.count(
                case((
                    (Order.check_in_date == today)
                    & (Order.order_status.in_(pre_checkin_statuses))
                    & (Order.is_deleted == False),
                    1,
                ))
            ).label("checkin_today"),
            func.count(
                case((
                    (Order.check_out_date == today)
                    & (Order.order_status.in_(pre_checkout_statuses))
                    & (Order.is_deleted == False),
                    1,
                ))
            ).label("checkout_today"),
        )
    )).one()

    # 续住组中间段：退房日=今天但客人不走（去下一段续住），不算「今日待退房」。
    # 真正待退房的是「组末段（活段口径）退房日=今天」。金额裁剪逻辑与此无关，不碰。
    from app.services import stay_group as _stay_group
    _mid_seg_ids: list[str] = []
    _grouped_today = (await db.execute(
        select(Order).where(
            Order.check_out_date == today,
            Order.order_status.in_(pre_checkout_statuses),
            Order.is_deleted == False,
            Order.stay_group_id.isnot(None),
        )
    )).scalars().all()
    for _o in _grouped_today:
        if not await _stay_group.is_group_last_segment(db, _o):
            _mid_seg_ids.append(_o.order_id)

    # 续住组非首段：入住日=今天但客人昨天就住进来了，前台不用办入住 → 不算「今日待入住」。
    # 与上面的退房中间段剔除对称：一趟续住只在首段入住日算一次待入住、只在末段退房日
    # 算一次待退房。生产 2026-07-25 sg_24b6b1aa12ba（1615 房）：续段停在 pending_confirm
    # 被当成待入住，前台反映「明明已经入住了还显示待入住」。
    _non_first_seg_ids: list[str] = []
    _grouped_ci_today = (await db.execute(
        select(Order).where(
            Order.check_in_date == today,
            Order.order_status.in_(pre_checkin_statuses),
            Order.is_deleted == False,
            Order.stay_group_id.isnot(None),
        )
    )).scalars().all()
    for _o in _grouped_ci_today:
        if not await _stay_group.is_group_first_segment(db, _o):
            _non_first_seg_ids.append(_o.order_id)

    # 「待保洁房间」= 今天真要打扫的退房房，与飞书每日打扫卡**同一个函数**算出来，
    # 保洁看到的和看板显示的永远一致。旧口径是「退房日=今天 且 cleaning_status 未派」，
    # 完全不看订单状态：取消单的 cleaning_status 停在默认 not_assigned 从不复位，于是
    # 全被算成待保洁（生产 2026-07-25：卡上 22 间，12 间取消 + 3 间已完成，真要扫的只有 7 间）。
    from app.services.cleaning_request import list_checkout_rooms_for_cleaning
    _cleaning_rows = await list_checkout_rooms_for_cleaning(db)

    # Overdue tasks (separate table) + total rooms
    overdue_tasks = await db.scalar(
        select(func.count())
        .select_from(Task)
        .where(Task.deadline < now_utc, Task.status != TaskStatus.done)
    ) or 0
    total_rooms = await db.scalar(select(func.count()).select_from(Room).where(Room.is_deleted == False)) or 0
    # 实时在住 = 此刻在店的「房间数」（不是订单数）。分母 total_rooms 是房间维度，分子也必须
    # 按房间数，否则一单多房会虚低（生产实盘 13 单/14 间 → 看板误显 13）。前端工具提示亦写明
    # 「此刻在住房数占比」。口径：checked_in 订单下、已排房(room_id 非空)、未单间退房
    # (checked_out_at 为空) 的 OrderRoom 行数。
    checked_in = await db.scalar(
        select(func.count())
        .select_from(OrderRoom)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .where(
            Order.order_status == OrderStatus.checked_in,
            Order.is_deleted == False,
            OrderRoom.room_id.isnot(None),
            OrderRoom.checked_out_at.is_(None),
        )
    ) or 0
    occupancy_rate = round(checked_in / total_rooms * 100, 1) if total_rooms > 0 else 0.0

    # 具体房号列表（前台卡片直显）。以 OrderRoom 逐房日期为准（迁移后的真相源），
    # 关联 Room.room_name；待排房（room_id=NULL）无房号、soft-deleted 单不计入。
    # 注：这是房号维度，可能与上面订单维度的计数不完全相等（一单多房时房号更多）。
    async def _room_names(*, date_col, statuses=None, exclude_order_ids=None) -> list[str]:
        q = (
            select(Room.room_name)
            .join(OrderRoom, OrderRoom.room_id == Room.room_id)
            .join(Order, Order.order_id == OrderRoom.order_id)
            .where(date_col == today, Order.is_deleted == False)
            .distinct()
        )
        if statuses is not None:
            q = q.where(Order.order_status.in_(statuses))
        if exclude_order_ids:
            q = q.where(Order.order_id.not_in(exclude_order_ids))
        names = (await db.execute(q)).scalars().all()
        return sorted(names)

    checkin_rooms = await _room_names(date_col=OrderRoom.check_in_date, statuses=pre_checkin_statuses,
                                      exclude_order_ids=_non_first_seg_ids)
    checkout_rooms = await _room_names(date_col=OrderRoom.check_out_date, statuses=pre_checkout_statuses,
                                       exclude_order_ids=_mid_seg_ids)
    cleaning_rooms = sorted({r["room_name"] for r in _cleaning_rows})

    return {
        "checkin_today": max((row.checkin_today or 0) - len(_non_first_seg_ids), 0),
        "checkout_today": max((row.checkout_today or 0) - len(_mid_seg_ids), 0),
        "cleaning_needed": len(_cleaning_rows),
        # 卡上的数字和抽屉里的名单来自同一份 _cleaning_rows，结构上不可能对不上。
        # （旧实现数字读 orders.cleaning_status、抽屉读 tasks 表，两个真相源。）
        "cleaning_list": [
            {
                "room_id": r["room_id"],
                "room_name": r["room_name"],
                "order_id": r["order_id"],
                "guest_name": r["guest_name"],
                "check_in_date": r["check_in_date"].isoformat(),
                "check_out_date": r["check_out_date"].isoformat(),
                "trial_tag": r["trial_tag"],
            }
            for r in _cleaning_rows
        ],
        "overdue_tasks": overdue_tasks,
        "checked_in": checked_in,
        "total_rooms": total_rooms,
        "occupancy_rate": occupancy_rate,
        "checkin_rooms": checkin_rooms,
        "checkout_rooms": checkout_rooms,
        "cleaning_rooms": cleaning_rooms,
    }


async def _period_stats(db, start_date: date, end_date: date, metrics: dict | None = None) -> dict:
    """任意区间 [start_date, end_date]（含端点）的财务汇总卡片。月度汇总只是
    start=月首 / end=月末 的特例。

    口径统一到 compute_period_metrics（批4 算法B）：营业额按订单、间夜按每行房(含待排房)
    各算、排携程占位单；整单按 check_out 离店日落区间。支出按 expense_date 落区间。
    OCC 分母 = 房间数 × 区间天数(含首尾)。
    ⚠️ 短区间下 OCC 可能 >100%：离店归属整单的口径特性——跨区间入住的整单间夜全落在
    离店那一侧，与月度/结算同源，属预期（月份长时不明显，选几天时会放大）。
    metrics 可由调用方(如 /overview)预算好传入，避免同段被重复全量聚合(评审 F3)。"""
    if metrics is None:
        metrics = await compute_period_metrics(db, start_date, end_date)
    channel_breakdown = {ch: v["order_count"] for ch, v in metrics["by_channel"].items()}

    total_expenses = float(await db.scalar(
        select(func.coalesce(func.sum(Expense.amount), 0)).where(
            Expense.expense_date >= start_date,
            Expense.expense_date <= end_date,
            Expense.is_deleted == False,
        )
    ) or 0)

    total_rooms = await db.scalar(select(func.count()).select_from(Room).where(Room.is_deleted == False)) or 0
    range_days = (end_date - start_date).days + 1   # 含首尾端点
    total_room_nights = total_rooms * range_days

    total_nights = int(metrics["room_nights"])
    total_revenue = float(metrics["revenue"])
    total_commission = float(metrics["commission"])
    total_net_revenue = total_revenue - total_commission

    occ = (total_nights / total_room_nights) if total_room_nights > 0 else 0
    adr = (total_revenue / total_nights) if total_nights > 0 else 0
    revpar = adr * occ

    return {
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "order_count": metrics["order_count"],
        "total_nights": total_nights,
        "total_actual_price": total_revenue,
        "total_commission": total_commission,
        "total_net_revenue": total_net_revenue,
        "total_expenses": total_expenses,
        "gross_profit": total_net_revenue - total_expenses,
        "occ": round(occ * 100, 2),
        "adr": round(adr, 2),
        "revpar": round(revpar, 2),
        "channel_breakdown": channel_breakdown,
    }


async def _monthly_stats(db, year: int, month: int, metrics: dict | None = None) -> dict:
    """整月汇总——_period_stats 的月首→月末特例，保留 year/month 字段供旧调用方。"""
    days_in_month = calendar.monthrange(year, month)[1]
    stats = await _period_stats(
        db, date(year, month, 1), date(year, month, days_in_month), metrics=metrics
    )
    return {"year": year, "month": month, **stats}


async def _revenue_trend(db, months: int) -> list[dict]:
    """Single aggregate query returning all monthly revenues at once."""
    today = today_cn()
    start = (today.replace(day=1) - relativedelta(months=months - 1))

    # 收入按离店月归属（2026-07-14 拍板）：跨月单整笔归离店月，与结算/财务月报一致。
    rows = (await db.execute(
        select(
            extract("year", Order.check_out_date).label("y"),
            extract("month", Order.check_out_date).label("m"),
            func.coalesce(func.sum(Order.actual_price), 0).label("revenue"),
        )
        .where(
            Order.is_deleted == False,
            Order.order_status != OrderStatus.cancelled,
            Order.check_out_date >= start,
        )
        .group_by("y", "m")
    )).all()

    by_key = {(int(y), int(m)): float(r) for y, m, r in rows}

    out = []
    for i in range(months - 1, -1, -1):
        target = today - relativedelta(months=i)
        out.append({
            "month": f"{target.year}-{str(target.month).zfill(2)}",
            "revenue": by_key.get((target.year, target.month), 0.0),
        })
    return out


async def _channel_analysis(db, year: int, month: int, metrics: dict | None = None) -> list[dict]:
    # 口径统一到 compute_period_metrics（批4 算法B）：间夜含待排房各房各算、排占位单。
    # metrics 可由 /overview 预算好传入，避免与 _monthly_stats 对同月重复聚合(评审 F3)。
    days_in_month = calendar.monthrange(year, month)[1]
    if metrics is None:
        metrics = await compute_period_metrics(db, date(year, month, 1), date(year, month, days_in_month))
    out = []
    for channel, v in metrics["by_channel"].items():
        order_count = v["order_count"]
        revenue = float(v["revenue"])
        commission = float(v["commission"])
        out.append({
            "channel": channel,
            "order_count": order_count,
            "total_revenue": revenue,
            "total_commission": commission,
            "net_revenue": revenue - commission,
            # 平均住晚用订单级住晚(评审 F4)：多房单不叠加，反映真实住店时长。
            "avg_nights": round(v["order_nights"] / order_count, 1) if order_count else 0,
            "avg_price": round(revenue / order_count, 2) if order_count else 0,
        })
    return out


async def _month_metrics_only(db, year: int, month: int, through_day: int | None = None) -> dict:
    """Lightweight variant for comparison — only the 4 KPIs we need.

    through_day: 若给定,只统计离店日(check_out) ≤ 当月 through_day 的订单(本月至今 MTD,
    含 through_day 当天;归属口径=离店日落月,与结算/财务一致),
    且 occ 分母改用已过天数(through_day)而非整月,保证与「上月同期」可比。
    None = 整月(月份已结束时用)。
    """
    # 口径统一到 compute_period_metrics（批4 算法B）。MTD：end 落到 through_day 那天。
    last_day = calendar.monthrange(year, month)[1]
    end_day = through_day if through_day is not None else last_day
    metrics = await compute_period_metrics(db, date(year, month, 1), date(year, month, min(end_day, last_day)))
    total_rooms = await db.scalar(select(func.count()).select_from(Room).where(Room.is_deleted == False)) or 0
    days = through_day if through_day is not None else last_day
    total_room_nights = total_rooms * days
    revenue = float(metrics["revenue"])
    nights = int(metrics["room_nights"])
    occ = round(nights / total_room_nights * 100, 2) if total_room_nights else 0
    adr = round(revenue / nights, 2) if nights else 0
    return {
        "year": year,
        "month": month,
        "revenue": revenue,
        "order_count": metrics["order_count"],
        "occupancy": occ,
        "adr": adr,
    }


async def _comparison(db, year: int, month: int) -> dict:
    def _calc_change(a, b):
        if not b:
            return None
        return round((a - b) / b * 100, 2)

    prev_date = date(year, month, 1) - relativedelta(months=1)

    # 环比同期口径：查询当月时只比到今天(MTD),对比月 clamp 到同一 day-of-month,
    # 避免「本月 7 天 vs 上月整月」的虚高。查询历史月份时月份已结束,整月对比。
    today = today_cn()
    is_mtd = (year == today.year and month == today.month)
    cutoff = today.day if is_mtd else None

    def _clamp(y: int, m: int) -> int | None:
        if cutoff is None:
            return None
        return min(cutoff, calendar.monthrange(y, m)[1])

    # Run the three independent month queries concurrently using separate sessions.
    # A single AsyncSession can't multiplex queries, so each branch opens its own
    # session from the pool — turns 3× sequential into 1× wall-clock.
    async def _isolated(y: int, m: int, through: int | None) -> dict:
        async with AsyncSessionLocal() as session:
            return await _month_metrics_only(session, y, m, through_day=through)

    current, last_month, same_ly = await asyncio.gather(
        _isolated(year, month, cutoff),
        _isolated(prev_date.year, prev_date.month, _clamp(prev_date.year, prev_date.month)),
        _isolated(year - 1, month, _clamp(year - 1, month)),
    )

    return {
        "is_mtd": is_mtd,
        "current": current,
        "last_month": last_month,
        "same_month_last_year": same_ly,
        "mom_change": {
            "revenue": _calc_change(current["revenue"], last_month["revenue"]),
            "order_count": _calc_change(current["order_count"], last_month["order_count"]),
            "occupancy": _calc_change(current["occupancy"], last_month["occupancy"]),
            "adr": _calc_change(current["adr"], last_month["adr"]),
        },
        "yoy_change": {
            "revenue": _calc_change(current["revenue"], same_ly["revenue"]),
            "order_count": _calc_change(current["order_count"], same_ly["order_count"]),
            "occupancy": _calc_change(current["occupancy"], same_ly["occupancy"]),
            "adr": _calc_change(current["adr"], same_ly["adr"]),
        },
    }


def _ensure_finance_role(current_user):
    if current_user["role"] not in ("admin", "finance"):
        raise HTTPException(status_code=403, detail="无权查看财务数据")


# ─── Partial-redaction for non-finance staff (operator / keeper) ───────────────
# 这些角色能看运营/房价指标（OCC/ADR/RevPAR/订单数/渠道占比），但金额类字段
# 必须在后端置 null——绝不下发，避免前端隐藏后仍能从响应里读到。
ROLES_WITH_METRICS = ("admin", "operator", "finance", "keeper")
ROLES_WITH_REVENUE = ("admin", "finance")

# ADR / RevPAR 属价格敏感，与金额一并对前台裁剪（2026-06-27 收紧）。
_MONEY_FIELDS_MONTHLY = (
    "total_actual_price",
    "total_commission",
    "total_net_revenue",
    "total_expenses",
    "gross_profit",
    "adr",
    "revpar",
)
_MONEY_FIELDS_CHANNEL = ("total_revenue", "total_commission", "net_revenue", "avg_price")


def _redact_monthly(monthly: dict) -> dict:
    for f in _MONEY_FIELDS_MONTHLY:
        monthly[f] = None
    return monthly


def _redact_channel(channel: list[dict]) -> list[dict]:
    for row in channel:
        for f in _MONEY_FIELDS_CHANNEL:
            row[f] = None
    return channel


def _redact_comparison(comparison: dict) -> dict:
    # revenue（金额）+ adr（价格）都对前台裁剪；occupancy / order_count 保留。
    for block in ("current", "last_month", "same_month_last_year", "mom_change", "yoy_change"):
        comparison[block]["revenue"] = None
        comparison[block]["adr"] = None
    return comparison


# ─── Public endpoints ─────────────────────────────────────────────────────────


@router.get("/overview")
async def dashboard_overview(
    db: DBSession,
    current_user: CurrentUser,
    year: int = Query(default_factory=lambda: today_cn().year),
    month: int = Query(default_factory=lambda: today_cn().month),
    months: int = Query(default=6, ge=1, le=24),
):
    """Batch endpoint: returns everything the dashboard page needs in one round trip."""
    role = current_user["role"]
    can_view_metrics = role in ROLES_WITH_METRICS
    can_view_revenue = role in ROLES_WITH_REVENUE

    # Sequential awaits — each internal query runs back-to-back, but we save
    # the 5 separate HTTP round trips from the frontend (the real win).
    today = await _today_stats(db)
    monthly = trend = channel = comparison = None
    if can_view_metrics:
        # 同月只算一次口径聚合，_monthly_stats 与 _channel_analysis 共享(评审 F3)。
        _days = calendar.monthrange(year, month)[1]
        _month_metrics = await compute_period_metrics(db, date(year, month, 1), date(year, month, _days))
        monthly = await _monthly_stats(db, year, month, metrics=_month_metrics)
        channel = await _channel_analysis(db, year, month, metrics=_month_metrics)
        comparison = await _comparison(db, year, month)
        if can_view_revenue:
            # Revenue trend is pure money — only finance/admin get it at all.
            trend = await _revenue_trend(db, months)
        else:
            # operator / keeper: keep metrics, strip every money field server-side.
            _redact_monthly(monthly)
            _redact_channel(channel)
            _redact_comparison(comparison)

    return {
        "today": today,
        "monthly": monthly,
        "trend": trend,
        "channel": channel,
        "comparison": comparison,
    }


@router.get("/today")
async def today_overview(db: DBSession, current_user: CurrentUser):
    if current_user["role"] not in ("admin", "operator", "finance", "keeper"):
        raise HTTPException(status_code=403, detail="无权查看今日概览")
    return await _today_stats(db)


@router.get("/monthly")
async def monthly_stats(
    db: DBSession,
    current_user: CurrentUser,
    year: int = Query(default_factory=lambda: today_cn().year),
    month: int = Query(default_factory=lambda: today_cn().month),
):
    _ensure_finance_role(current_user)
    # 月度归属说明：按 check_out_date 离店月计（2026-07-14 拍板）；跨月订单（如 6/30 入住、
    # 7/1 退房）整笔归 7 月。对应 settlements/finance/owner_portal 同口径，前后端一致。
    # 支出侧另按 expense_date 归月。如需改成按夜数分摊请同步全部收入口径处。
    return await _monthly_stats(db, year, month)


@router.get("/period-summary")
async def period_summary(
    db: DBSession,
    current_user: CurrentUser,
    start_date: date = Query(..., description="起始日期 YYYY-MM-DD"),
    end_date: date = Query(..., description="结束日期 YYYY-MM-DD"),
):
    """任意起止日期的财务汇总卡片（月度汇总的区间版）。
    收入/佣金/间夜按订单离店日落区间、支出按发生日落区间（口径同月度汇总，仅日期粒度更细）。
    finance-only（admin/finance），前台角色不下发金额，故不走 /overview 的裁剪链路。"""
    _ensure_finance_role(current_user)
    if end_date < start_date:
        raise HTTPException(status_code=400, detail="end_date 必须 >= start_date")
    return await _period_stats(db, start_date, end_date)


@router.get("/revenue-trend")
async def revenue_trend(
    db: DBSession,
    current_user: CurrentUser,
    months: int = Query(default=6, ge=1, le=24),
):
    _ensure_finance_role(current_user)
    return await _revenue_trend(db, months)


@router.get("/channel-analysis")
async def channel_analysis(
    db: DBSession,
    current_user: CurrentUser,
    year: int = Query(default_factory=lambda: today_cn().year),
    month: int = Query(default_factory=lambda: today_cn().month),
):
    _ensure_finance_role(current_user)
    return await _channel_analysis(db, year, month)


@router.get("/comparison")
async def comparison_report(
    db: DBSession,
    current_user: CurrentUser,
    year: int = Query(default_factory=lambda: today_cn().year),
    month: int = Query(default_factory=lambda: today_cn().month),
):
    _ensure_finance_role(current_user)
    return await _comparison(db, year, month)
