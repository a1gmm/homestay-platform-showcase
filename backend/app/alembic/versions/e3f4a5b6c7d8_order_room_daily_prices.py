"""order_rooms add daily_prices JSONB + backfill

Revision ID: e3f4a5b6c7d8
Revises: d2e3f4a5b6c7
Create Date: 2026-05-25 22:50:00.000000

王总 2026-05-25 反馈：订单详情需要看到每天的房价，并支持单独修改某一天的价格。

实现策略：order_rooms 表加 daily_prices JSONB 列（默认 {}），保存 { "YYYY-MM-DD": "253.38" }。
- 新创建/整体替换 OrderRoom 时按 actual_price/nights 均摊（首日吸收四舍五入误差）
- 单日 PATCH 后 actual_price = sum(daily_prices)
- 历史存量订单跑一次 backfill 自动均摊

值用 string 存（JSONB 直接存 Decimal 会被 asyncpg 拒绝，存 float 会丢精度）。
"""
from typing import Sequence, Union
from datetime import timedelta
from decimal import Decimal

from alembic import op
import sqlalchemy as sa


revision: str = 'e3f4a5b6c7d8'
down_revision: Union[str, None] = 'd2e3f4a5b6c7'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_TWO_PLACES = Decimal("0.01")


def _compute_daily_prices(check_in, check_out, total_price):
    """与 services/order_pricing.py:compute_daily_prices 等价（migration 不 import 业务代码）。"""
    if total_price is None:
        return {}
    nights = (check_out - check_in).days
    if nights <= 0:
        return {}
    total = Decimal(total_price).quantize(_TWO_PLACES)
    avg = (total / nights).quantize(_TWO_PLACES)
    dates = [check_in + timedelta(days=i) for i in range(nights)]
    result = {d.isoformat(): str(avg) for d in dates}
    result[dates[0].isoformat()] = str(total - avg * (nights - 1))
    return result


def upgrade() -> None:
    # 1) 加列
    op.add_column(
        "order_rooms",
        sa.Column(
            "daily_prices",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )

    # 2) 回填存量订单：每行按 actual_price/nights 均摊
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT order_room_id, check_in_date, check_out_date, actual_price "
            "FROM order_rooms"
        )
    ).fetchall()

    import json
    for r in rows:
        daily = _compute_daily_prices(r.check_in_date, r.check_out_date, r.actual_price)
        if not daily:
            continue
        conn.execute(
            sa.text(
                "UPDATE order_rooms SET daily_prices = CAST(:dp AS JSONB) "
                "WHERE order_room_id = :id"
            ),
            {"dp": json.dumps(daily), "id": r.order_room_id},
        )


def downgrade() -> None:
    op.drop_column("order_rooms", "daily_prices")
