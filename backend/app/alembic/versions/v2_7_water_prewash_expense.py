"""v2#7 add expense categories: water + new_linen_prewash

Revision ID: v2_7_water_prewash
Revises: f8b3d5a1c9e2
Create Date: 2026-07-19

对齐王总《业主分成明细》，新增两个支出类别 enum 值：
- water            水费（合并原冷水 cold_water + 热水 hot_water；后两者停用只留历史读）
- new_linen_prewash 新布草过水费

采购费复用已存在的 supplies 值（无需 ADD VALUE）。

注意：PostgreSQL ALTER TYPE ADD VALUE 不能在事务内执行，故在 autocommit_block 里。
新值的默认占比规则由下一个迁移 (v2_7_seed_water_proc) seed —— 必须分两个迁移，
因为新 enum 值不能在"添加它的同一个事务"里被 INSERT 引用。

revision id 用可读串（非 hex），规避多分支手编 hex id 撞号（曾撞 c1d2e3f4a5b6）。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "v2_7_water_prewash"
down_revision: Union[str, None] = "f8b3d5a1c9e2"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_EXPENSE_CATEGORIES = [
    "water",
    "new_linen_prewash",
]


def upgrade() -> None:
    with op.get_context().autocommit_block():
        for value in NEW_EXPENSE_CATEGORIES:
            op.execute(
                f"ALTER TYPE expense_category ADD VALUE IF NOT EXISTS '{value}'"
            )


def downgrade() -> None:
    # PG 不能从 enum 移除已 ADD 的 value —— 与既有迁移一致，downgrade 为 no-op。
    pass
