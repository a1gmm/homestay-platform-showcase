"""expand channel enum: 10 active channels + legacy data migration

Revision ID: c1d2e3f4a5b6
Revises: f4a8c1b3e7d2
Create Date: 2026-05-23 10:00:00.000000

王总团队 2026-05-23 反馈：渠道字典扩展到 10 个 active 渠道
(美团酒店/抖音/线下渠道/自来客/携程/去哪儿/智行/同程/美团民宿/自住)。

策略：
1. PG ENUM 加 9 个新值（ADD VALUE 必须 autocommit_block，不能事务回滚）
2. 数据迁移：walk_in→offline、private/direct→self_acquired
3. meituan / tujia 老值不动 — 没法自动区分美团酒店 vs 民宿，
   tujia 客户已弃用但保留供历史订单显示。前端下拉只暴露 10 个新值。
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c1d2e3f4a5b6'
down_revision: Union[str, None] = 'f4a8c1b3e7d2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_CHANNEL_VALUES = [
    'meituan_hotel',
    'meituan_homestay',
    'douyin',
    'qunar',
    'zhixing',
    'tongcheng',
    'self_acquired',
    'offline',
    'self_used',
]


def upgrade() -> None:
    # 1) ADD VALUE 必须在 autocommit 块内（PG 限制）
    with op.get_context().autocommit_block():
        for value in NEW_CHANNEL_VALUES:
            op.execute(f"ALTER TYPE channel ADD VALUE IF NOT EXISTS '{value}'")

    # 2) 历史数据迁移（语义明确的三个老值）
    op.execute("UPDATE orders SET channel='offline' WHERE channel='walk_in'")
    op.execute("UPDATE orders SET channel='self_acquired' WHERE channel IN ('private', 'direct')")
    # meituan / tujia 不动：meituan 无法自动判定酒店 vs 民宿；tujia 保留显示


def downgrade() -> None:
    # PG ENUM 不能删值。downgrade 仅做数据回滚（best-effort）。
    op.execute("UPDATE orders SET channel='walk_in' WHERE channel='offline'")
    # private/direct 合并到 self_acquired 后无法区分，统一回到 private
    op.execute("UPDATE orders SET channel='private' WHERE channel='self_acquired'")
