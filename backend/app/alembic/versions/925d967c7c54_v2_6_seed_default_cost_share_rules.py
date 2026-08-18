"""v2#6 seed default cost-share rules

Revision ID: 925d967c7c54
Revises: 075db9ba3b94
Create Date: 2026-05-09 23:56:09.637458

为现有所有房间 seed 默认 RoomCostShareRule（业主全担 share_percent=1.0），覆盖：
- 3 个 booking_type (normal/trial/owner_self)
- 14 个 OWNER_COST_SHARE_CATEGORIES（按王总参考截图顺序）
- 33 房 × 3 × 14 = 1386 行（数量随房间数变化）

幂等：用 ON CONFLICT (room_id, booking_type, expense_category) DO NOTHING，
迁移可重复跑 + 后续新增的房间也能补 seed（admin 在 UI 编辑时如果没规则会
自动 fallback 到默认 1.0，不严格依赖此 seed；但 seed 让结算 query 命中规则
而非 fallback 路径，更可靠）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '925d967c7c54'
down_revision: Union[str, None] = '075db9ba3b94'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


# v2 业主费用占比 14 类目（与 backend/app/models/expense.py OWNER_COST_SHARE_CATEGORIES 一致）
COST_SHARE_CATEGORIES = [
    "cleaning",
    "public_utilities",
    "cold_water",
    "broadband",
    "daily_supplies",
    "laundry",
    "hot_water",
    "gas",
    "property_fee",
    "electricity",
    "maintenance",
    "other",
    "kitchen_cleaning",
    "property_guidance_fee",
]
BOOKING_TYPES = ["normal", "trial", "owner_self"]


def upgrade() -> None:
    # 用 SQL 直接 INSERT，避开 ORM session（migration 阶段不应触发 ORM）
    # rule_id 用 'CSR-' + md5(room_id || booking_type || category)[:12].upper() 保证幂等
    op.execute(
        f"""
        INSERT INTO room_cost_share_rules (rule_id, room_id, booking_type, expense_category, share_percent)
        SELECT
            'CSR-' || UPPER(SUBSTRING(MD5(r.room_id || '|' || bt || '|' || cat) FROM 1 FOR 12)) AS rule_id,
            r.room_id,
            bt::booking_type AS booking_type,
            cat::expense_category AS expense_category,
            1.000 AS share_percent
        FROM rooms r
        CROSS JOIN UNNEST(ARRAY{BOOKING_TYPES}::text[]) AS bt
        CROSS JOIN UNNEST(ARRAY{COST_SHARE_CATEGORIES}::text[]) AS cat
        ON CONFLICT (room_id, booking_type, expense_category) DO NOTHING
        """
    )


def downgrade() -> None:
    # 删除所有 share_percent=1.000 + unit_price IS NULL 的"默认值"行，但保留人工改过的
    # （注意：业主不应该回滚到 v1，所以 downgrade 只是兜底，不必清得很干净）
    op.execute(
        """
        DELETE FROM room_cost_share_rules
        WHERE share_percent = 1.000 AND unit_price IS NULL
        """
    )
