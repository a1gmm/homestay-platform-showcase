"""订单每日房价工具。

每行 OrderRoom 维护一个 daily_prices JSONB 字段 { "YYYY-MM-DD": "253.38" }。
- 创建/整体替换时按 actual_price/nights 均摊，首日吸收四舍五入误差（保证 sum == actual_price）
- 单日 PATCH 后 actual_price = sum(daily_prices.values())
- 值用 string 存（JSONB 直接存 Decimal 会被 asyncpg 拒绝；存 float 会丢精度）
"""
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, InvalidOperation
from typing import Iterable


_TWO_PLACES = Decimal("0.01")
_FOUR_PLACES = Decimal("0.0001")


def order_room_commission(
    actual_price: Decimal | None,
    per_room_net: Decimal | None,
    order_commission_rate: Decimal | None,
) -> Decimal:
    """单行 OrderRoom 的平台佣金——每房净房费口径唯一真相（B 方案 2026-07-04）。

    - 该行手填了每房净房费（且有房费）：佣金 = 房费 − 每房净值（携程同单各房佣金比例可不同）。
    - 未手填 / 房费缺失：佣金 = 房费 × 整单佣金率（与旧 SQL 聚合等价，比例单两算法同值）。

    结算/业主门户/财务月报三处必须共用本函数，避免口径分叉（评审 2026-07-05）。
    duck-typed 传值即可。房费缺失(None/≤0)时即便有 net 也走比例分支，避免
    net 被从 0 里减出负佣金、虚增业主收入。
    """
    price = Decimal(actual_price or 0)
    if per_room_net is not None and price > 0:
        return price - per_room_net
    return price * Decimal(order_commission_rate or 0)


def effective_owner_revenue_for_room(
    per_room_net: "Decimal | None",
    actual_price: "Decimal | None",
    order_metadata: dict | None,
    order_room_count: int | None,
) -> "Decimal | None":
    """结算/业主门户/财务月报统一解析"某 OrderRoom 行的业主原始到手净值"，
    结果喂给 order_room_commission 当 per_room_net 用。

    取值优先级：
    1) 手填每房净房费(per_room_net) → 直接用（多房单各房佣金比例可不同）。
    2) 否则**单房单**(order_room_count==1) 且订单级冻结平台到手
       metadata.ota_owner_revenue（= Order.expected_revenue，页面「净房费」那列）
       存在、且 ≤ 房费 → 用它。让平台单直接按"原始到手"结算，不再由 4 位佣金率
       倒推（(实收−到手)/实收 四舍五入成 4 位会磨损 1 分，王总 2026-07-14 拍板）。
       到手 > 房费 是 OTA 倒贴补贴单（ota_subsidy 另计），这里不接管，返回 None
       走比例分支，避免负佣金 + 补贴双算虚增业主收入。
    3) 都不满足 → None（order_room_commission 回退 房费×整单佣金率，行为不变）。

    只对单房单接管：多房单的订单级到手横跨多间无法归到单行，保持 per-room 手填/比例回退。
    duck-typed：传值即可，不依赖 ORM 对象。
    """
    if per_room_net is not None:
        return per_room_net
    if order_room_count == 1:
        raw = (order_metadata or {}).get("ota_owner_revenue")
        if raw not in (None, ""):
            try:
                val = Decimal(str(raw))
            except (InvalidOperation, ValueError):
                return None
            price = Decimal(actual_price or 0)
            # val 必须 > 0：ota_owner_revenue 存成 0/负（自建单/脏同步残留）不是"业主到手=0"，
            # 若当有效值 → 佣金=房费−0=全额房费、业主被清零。与导出口径 (0<er≤实收) 对齐，
            # ≤0 一律回退比例分支（2026-07-15）。
            if price > 0 and Decimal("0") < val <= price:
                return val
    return None


def clear_per_room_owner_revenue(order, *, reason: str, extra: dict | None = None) -> dict | None:
    """清掉订单各房行手填的每房净房费（回退比例口径），并把清值事件**追加**到
    order.metadata['per_room_owner_revenue_cleared_log'] 列表留痕（多次清值不互相覆盖，
    保住审计链）；同时保留最近一次到 'per_room_owner_revenue_cleared' 便于旧读取方。

    换房 / 单日改价 / 整单净房费改动与 Σ每房冲突时调用，避免结算拿旧每房值静默算错。
    返回被清的 {order_room_id: 旧净值str} 或 None（无手填值时）。
    """
    manual = {
        r.order_room_id: str(r.ota_owner_revenue)
        for r in order.rooms if r.ota_owner_revenue is not None
    }
    if not manual:
        return None
    for r in order.rooms:
        if (r.metadata_ or {}).get("ota_owner_revenue") is not None:
            r.metadata_ = {k: v for k, v in r.metadata_.items() if k != "ota_owner_revenue"}
    entry = {"old": manual, "reason": reason, "at": datetime.now(timezone.utc).isoformat()}
    if extra:
        entry.update(extra)
    meta = dict(order.metadata_ or {})
    log = list(meta.get("per_room_owner_revenue_cleared_log") or [])
    log.append(entry)
    meta["per_room_owner_revenue_cleared_log"] = log
    meta["per_room_owner_revenue_cleared"] = entry  # 最近一次（兼容旧读取方）
    order.metadata_ = meta
    return manual


def safe_decimal(raw, default: Decimal = Decimal("0")) -> Decimal:
    """把 metadata 里的任意值安全转 Decimal；脏值（空串/含逗号/None 字面量）返回 default，
    不抛异常——用于结算等批处理，避免单条脏数据 500 掉整批。"""
    if raw is None:
        return default
    try:
        return Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return default


def derive_ota_commission_rate(
    actual_total: Decimal | None, ota_owner_revenue: Decimal | None
) -> Decimal | None:
    """根子统一：由"实收"与"携程实际到手"反推平台佣金率，使
    net_revenue = 实收 × (1 − rate) ≡ 携程到手。与 ota-sync 导入口径一致
    (rate = (实收 − 到手)/实收，负率夹 0，量化 4 位)。

    缺实收/缺到手/实收≤0(占位单) → None，调用方据此"不动佣金率"。
    """
    if actual_total is None or ota_owner_revenue is None or actual_total <= 0:
        return None
    rate = (actual_total - ota_owner_revenue) / actual_total
    return max(Decimal("0"), rate).quantize(_FOUR_PLACES, rounding=ROUND_HALF_UP)


def sync_ota_commission_rate(order) -> bool:
    """改单后保持 OTA 单佣金率：让 net_revenue ≡ 携程实际到手。

    仅当订单有 metadata.ota_owner_revenue（OTA 搬单 / bypms-import 等，与
    expected_revenue 同口径）且 actual_price>0 时，按 derive_ota_commission_rate
    重设 order.platform_commission_rate。手填单（无该字段）/占位单（actual<=0）
    一律不动，保留运营手填佣金率。返回是否改动了佣金率。

    duck-typed：只读 order.metadata_ / order.actual_price，写 order.platform_commission_rate，
    不 import Order 模型（避免循环依赖）。
    """
    raw = (getattr(order, "metadata_", None) or {}).get("ota_owner_revenue")
    if raw in (None, ""):
        return False
    try:
        revenue = Decimal(str(raw))
    except (InvalidOperation, ValueError):
        return False
    rate = derive_ota_commission_rate(order.actual_price, revenue)
    if rate is None:
        return False
    order.platform_commission_rate = rate
    return True


def compute_daily_prices(
    check_in: date,
    check_out: date,
    total_price: Decimal | None,
) -> dict[str, str]:
    """按 nights 均摊到每一晚，首日吸收四舍五入误差。

    返回 { "YYYY-MM-DD": "253.38" }（值是 string）。
    total_price 为 None 或 nights <= 0 时返回 {}（待定价订单不预填）。
    """
    if total_price is None:
        return {}
    nights = (check_out - check_in).days
    if nights <= 0:
        return {}

    total = Decimal(total_price).quantize(_TWO_PLACES)
    avg = (total / nights).quantize(_TWO_PLACES)
    dates = [check_in + timedelta(days=i) for i in range(nights)]
    # 默认每晚均价
    result = {d.isoformat(): str(avg) for d in dates}
    # 首日吸收四舍五入误差，保证 sum 严格 == total（避免出现 ¥0.01-0.03 漂移）
    first_day_price = total - avg * (nights - 1)
    result[dates[0].isoformat()] = str(first_day_price)
    return result


def sum_daily_prices(daily_prices: dict[str, str | Decimal | float | int] | None) -> Decimal:
    """求和 daily_prices，作为 OrderRoom.actual_price 的真值。"""
    if not daily_prices:
        return Decimal("0")
    total = Decimal("0")
    for v in daily_prices.values():
        total += Decimal(str(v))
    return total.quantize(_TWO_PLACES)


def daily_dates_in_range(check_in: date, check_out: date) -> Iterable[date]:
    """生成入住区间内的每一晚（左闭右开，nights 天）。"""
    n = (check_out - check_in).days
    for i in range(n):
        yield check_in + timedelta(days=i)


def split_daily_prices_by_date(
    daily_prices: dict[str, str], split_date: date
) -> tuple[dict[str, str], dict[str, str]]:
    """把 daily_prices 按 split_date 切成 (before, after) 两段（左闭右开）。
    before = 日期 < split_date 的晚；after = 日期 >= split_date 的晚。
    用于入住中换房拆分 OrderRoom。两段并集 == 原 dict，和不变。"""
    split_key = split_date.isoformat()
    before = {k: v for k, v in (daily_prices or {}).items() if k < split_key}
    after = {k: v for k, v in (daily_prices or {}).items() if k >= split_key}
    return before, after
