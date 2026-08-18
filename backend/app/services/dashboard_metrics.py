"""看板周期指标的单一口径真相源（批4，王总 2026-07-11 拍板「算法B」）。

口径：
  - 营业额 revenue   = Σ 订单 actual_price（按订单总额，不按房重复计）
  - 佣金   commission= Σ 订单 actual_price × 整单佣金率
  - 间夜   room_nights = Σ 每一行 OrderRoom 的 (退房−入住) 天数——**含待排房**
    (room_id NULL)，多房订单每间房各算；这是修「多房订单被少算」的核心。
  - 排除：已删 / 已取消 / 携程 ¥0 占位单(price_pending)。
  - 归属：整笔按「离店日期」落月（2026-07-14 拍板）——营业额按订单 check_out 落月，
    间夜按每行 OrderRoom check_out 落月，跨月单整笔归离店月（例 6/30 入住 7/1 离店
    → 7 月），与结算/财务月报/业主门户口径一致。支出侧另按 expense_date 归月。

为什么在 Python 算间夜：SQLite 测试库 `check_out - check_in` 日期相减恒为 0
（见 test_dashboard_comparison_mtd 注释），SQL 相减的间夜无法单测；Python 算
两库通用且可测。周期订单量不大（百级），全量拉取聚合无性能顾虑。
"""
from datetime import date
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.models.order import Order, OrderStatus, StaySettlementKind
from app.models.order_room import OrderRoom


def _channel_key(channel) -> str:
    return channel.value if hasattr(channel, "value") else str(channel)


async def compute_period_metrics(db, start_date: date, end_date: date) -> dict:
    """算 [start_date, end_date]（含端点，按 check_out 离店日落月）的看板指标。返回 dict。"""
    def _blank():
        # room_nights: 按房×晚(含待排房)——占房口径，给在住率/ADR 用。
        # order_nights: 按订单住晚(退房−入住)——住店时长口径，给渠道「平均住晚」用，
        #   多房单不叠加（否则 3 房×2 晚会显示"平均住 6 晚"，评审 F4）。
        return {"order_count": 0, "revenue": Decimal("0"),
                "commission": Decimal("0"), "room_nights": 0, "order_nights": 0}

    total = _blank()
    by_channel: dict[str, dict] = {}

    def _bucket(ch: str) -> dict:
        return by_channel.setdefault(ch, _blank())

    # ── 营业额/佣金/订单数：按订单 check_out 离店日落月，排占位单 ──
    order_rows = (await db.execute(
        select(Order).where(
            Order.is_deleted == False,
            Order.order_status != OrderStatus.cancelled,
            Order.check_out_date >= start_date,
            Order.check_out_date <= end_date,
        )
    )).scalars().all()
    for o in order_rows:
        if o.price_pending:   # 携程 ¥0 占位单不计
            continue
        ch = _channel_key(o.channel)
        is_company_sponsored = (
            o.stay_settlement_kind == StaySettlementKind.company_sponsored
        )
        # 公司承担是业主结算收入，不是客人收款/营业额；历史脏 actual_price 也显式隔离。
        rev = Decimal("0") if is_company_sponsored else Decimal(o.actual_price or 0)
        # 佣金与订单属性/导出/结算同口径（2026-07-14）：OTA 单按「实收−原始到手」倒算并
        # 量化到分，非 OTA 单按 实收×佣金率量化。旧写法 rev×rate 既不量化(汇总卡片出现
        # 3 位小数)又对 OTA 单磨损 1 分，且与导出明细对不上。o.platform_commission 复用
        # 同一属性，只读 metadata/actual_price，无需预加载 rooms。
        comm = Decimal("0") if is_company_sponsored else o.platform_commission
        stay = 0
        if o.check_in_date and o.check_out_date:
            stay = max(0, (o.check_out_date - o.check_in_date).days)
        for tgt in (total, _bucket(ch)):
            tgt["order_count"] += 1
            tgt["revenue"] += rev
            tgt["commission"] += comm
            tgt["order_nights"] += stay

    # ── 间夜：按每行 OrderRoom check_out 离店日落月，含待排房，排占位单/取消/删 ──
    room_rows = (await db.execute(
        select(OrderRoom, Order.channel, Order.metadata_)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .where(
            Order.is_deleted == False,
            Order.order_status != OrderStatus.cancelled,
            OrderRoom.check_out_date >= start_date,
            OrderRoom.check_out_date <= end_date,
        )
    )).all()
    for orr, channel, meta in room_rows:
        raw = (meta or {}).get("price_pending")
        if raw is True or str(raw).lower() == "true":   # 占位单不计
            continue
        if not orr.check_out_date or not orr.check_in_date:
            continue
        nights = (orr.check_out_date - orr.check_in_date).days
        if nights <= 0:
            continue
        total["room_nights"] += nights
        _bucket(_channel_key(channel))["room_nights"] += nights

    return {**total, "by_channel": by_channel}
