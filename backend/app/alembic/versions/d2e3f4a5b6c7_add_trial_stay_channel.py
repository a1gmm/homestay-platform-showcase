"""add trial_stay channel value

Revision ID: d2e3f4a5b6c7
Revises: c1d2e3f4a5b6
Create Date: 2026-05-25 22:45:00.000000

王总 2026-05-25 反馈：新建订单渠道里加"试住"。
trial_stay 与 self_used 语义不同：
- self_used = 内部员工占用 / 自住
- trial_stay = 潜在客户体验房（前期销售试睡）
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'd2e3f4a5b6c7'
down_revision: Union[str, None] = 'c1d2e3f4a5b6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PG 限制：ADD VALUE 必须在 autocommit 块内，不能事务回滚。
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE channel ADD VALUE IF NOT EXISTS 'trial_stay'")


def downgrade() -> None:
    # PG ENUM 不能删值；如有 trial_stay 订单，回滚到 self_used 兜底（避免约束破坏）。
    op.execute("UPDATE orders SET channel='self_used' WHERE channel='trial_stay'")
