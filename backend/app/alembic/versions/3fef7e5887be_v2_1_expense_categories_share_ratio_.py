"""v2#1 expense categories + share ratio template enum

Revision ID: 3fef7e5887be
Revises: dc4493008dc2
Create Date: 2026-05-09 23:39:40.428748

王总参考 PMS 系统，扩展业主财务模型：
1. ExpenseCategory 从 7 类扩到 18 类（保留旧类兼容 + 新增 11 类细分项）
2. 新建 share_ratio_template enum（owner_10..owner_full / none / custom）

注意：PostgreSQL ALTER TYPE ADD VALUE 不能在事务内执行，故所有 enum
add value 调用都在 autocommit_block 里。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '3fef7e5887be'
down_revision: Union[str, None] = 'dc4493008dc2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# 11 个新增 ExpenseCategory enum value（按王总截图顺序）
NEW_EXPENSE_CATEGORIES = [
    "public_utilities",
    "cold_water",
    "broadband",
    "daily_supplies",
    "laundry",
    "hot_water",
    "gas",
    "property_fee",
    "electricity",
    "kitchen_cleaning",
    "property_guidance_fee",
]

# share_ratio_template enum 全部值
SHARE_RATIO_TEMPLATE_VALUES = [
    "owner_10", "owner_20", "owner_30", "owner_40", "owner_50",
    "owner_60", "owner_70", "owner_80", "owner_90",
    "owner_full", "none", "custom",
]


def upgrade() -> None:
    # ① 扩 ExpenseCategory enum — ALTER TYPE ADD VALUE 必须 autocommit
    with op.get_context().autocommit_block():
        for value in NEW_EXPENSE_CATEGORIES:
            op.execute(
                f"ALTER TYPE expense_category ADD VALUE IF NOT EXISTS '{value}'"
            )

    # ② 创建 share_ratio_template enum（普通 CREATE TYPE 可以在事务里跑）
    share_ratio_template_enum = sa.Enum(
        *SHARE_RATIO_TEMPLATE_VALUES, name="share_ratio_template"
    )
    share_ratio_template_enum.create(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    # 不能从 enum 移除已 ADD 的 value（PG 限制）。回滚仅删 share_ratio_template。
    # 老数据兼容字段名仍保留，无需 drop column。
    sa.Enum(name="share_ratio_template").drop(op.get_bind(), checkfirst=True)
