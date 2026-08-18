"""订单相关 ID 生成（单一来源）。

历史上 orders.gen_order_id 与 booking._gen_order_id 是两份逐字节重复实现
（仅 C 前缀差异）且漂移过一次，现收敛到这里；调用点：admin 建单（orders）、
C 端预订（booking，prefix="C"）、bypms 补建（reconciliation）。

后缀 6 位 hex：orders.order_id 是 String(20)，C 端格式 ORD-YYYYMMDD-CXXXXXX
恰好 20 字符，6 位是上限。
"""
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_helpers import today_cn


def gen_order_id(prefix: str = "") -> str:
    # 日期前缀用北京时间——UTC 会让北京 0-8 点的单拿到前一天（#191）
    today = today_cn().strftime("%Y%m%d")
    return f"ORD-{today}-{prefix}{uuid4().hex[:6].upper()}"


def gen_order_room_id() -> str:
    return "OR-" + uuid4().hex[:20].upper()


async def unique_order_id(db: AsyncSession, prefix: str = "", max_attempts: int = 5) -> str:
    """生成未被占用的订单号：查库撞号则重新生成。

    查库与插入之间仍有竞争窗口，order_id 唯一约束是最终兜底；6 位 hex 下
    同日碰撞约百年一遇，这里只兜「查得到的」冲突，不做事务级重试。
    """
    from app.models.order import Order  # 延迟导入避免 core ↔ models 环
    for _ in range(max_attempts):
        oid = gen_order_id(prefix)
        taken = (await db.execute(
            select(Order.order_id).where(Order.order_id == oid).limit(1)
        )).scalar_one_or_none()
        if taken is None:
            return oid
    raise RuntimeError(f"订单号生成 {max_attempts} 次全部撞号（prefix={prefix!r}），请重试")
