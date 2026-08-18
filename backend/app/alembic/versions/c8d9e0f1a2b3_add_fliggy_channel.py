"""add fliggy channel value

Revision ID: c8d9e0f1a2b3
Revises: b6c7d8e9f0a1
Create Date: 2026-06-12 10:00:00.000000

王总 2026-06-12 反馈：订单预定渠道里加"飞猪"。
fliggy = 阿里系 OTA 平台（飞猪），与携程/美团同类，属平台型渠道（算佣金）。
"""
from typing import Sequence, Union

from alembic import op


revision: str = 'c8d9e0f1a2b3'
down_revision: Union[str, None] = 'b6c7d8e9f0a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # PG 限制：ADD VALUE 必须在 autocommit 块内，不能事务回滚。
    with op.get_context().autocommit_block():
        op.execute("ALTER TYPE channel ADD VALUE IF NOT EXISTS 'fliggy'")


def downgrade() -> None:
    # PG ENUM 不能删值；如有 fliggy 订单，回滚到 offline 兜底（避免约束破坏）。
    op.execute("UPDATE orders SET channel='offline' WHERE channel='fliggy'")
