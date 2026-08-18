"""单房单房费镜像自愈：order_rooms.actual_price 恒等于 orders.actual_price。

背景（2026-07-14）：月度对账用一次性脚本直接 `UPDATE orders SET actual_price=…` 抬高
房费，却没同步 `order_rooms.actual_price` → 业主门户/结算按每房价聚合时读到旧值、房费
虚低（6 月漂了 116 单 / ¥6238）。正常 app 路径（改单/单日改价/换房）本就两侧同步，唯有
绕过 app 的裸 SQL 脚本会漂。此自愈定时把**单房单**的每房价拉回订单级真值——单房单里
房 = 单，二者定义上恒等。多房单不碰（各房拆分合法，订单级横跨多间无法归到单行）。
"""
from __future__ import annotations

from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.services.order_pricing import compute_daily_prices


async def selfheal_single_room_prices(db: AsyncSession, *, limit: int | None = None) -> int:
    """把单房单里 actual_price 与订单不一致的 order_room 拉回订单级真值，重算 daily_prices。

    只处理**未删除、非取消**订单里恰好 1 行 order_room 的单。返回修正的行数（幂等：
    已一致的不动、重复跑不叠加）。多房单一律跳过。"""
    single_ids = (
        await db.execute(
            select(OrderRoom.order_id)
            .group_by(OrderRoom.order_id)
            .having(func.count() == 1)
        )
    ).scalars().all()
    if not single_ids:
        return 0

    rows = (
        await db.execute(
            select(OrderRoom, Order.actual_price)
            .join(Order, Order.order_id == OrderRoom.order_id)
            .where(Order.is_deleted == False)
            .where(Order.order_status != OrderStatus.cancelled)
            .where(OrderRoom.order_id.in_(single_ids))
        )
    ).all()

    fixed = 0
    for orm, order_actual in rows:
        if order_actual is None:
            continue
        cur = Decimal(orm.actual_price if orm.actual_price is not None else 0)
        if cur == Decimal(order_actual):
            continue
        orm.actual_price = order_actual
        orm.daily_prices = compute_daily_prices(
            orm.check_in_date, orm.check_out_date, order_actual
        )
        fixed += 1
        if limit is not None and fixed >= limit:
            break
    return fixed
