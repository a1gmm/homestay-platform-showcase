"""v2#7 seed default cost-share rules for water / new_linen_prewash / supplies

Revision ID: v2_7_seed_water_proc
Revises: v2_7_water_prewash
Create Date: 2026-07-19

给所有现有房间 seed 三个新入占比清单的类目默认规则（业主全担 share_percent=1.000）：
- water            水费
- new_linen_prewash 新布草过水费
- supplies         采购费（原"用品(旧)"复用启用）

照抄 925d967c7c54 的写法：INSERT ... SELECT ... CROSS JOIN UNNEST ... ON CONFLICT DO NOTHING。
默认 1.000 与既有 seed 一致，且结算 query 缺规则时本就 fallback 到 1.0，故不改动任何
已有房间的分成口径——只是让新类目走"命中规则"而非 fallback 路径，更可靠。

依赖前一个迁移已提交 water/new_linen_prewash 两个 enum 值（PG 要求分事务）。
"""
from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "v2_7_seed_water_proc"
down_revision: Union[str, None] = "v2_7_water_prewash"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_COST_SHARE_CATEGORIES = [
    "water",
    "new_linen_prewash",
    "supplies",
]
BOOKING_TYPES = ["normal", "trial", "owner_self"]


def upgrade() -> None:
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
        CROSS JOIN UNNEST(ARRAY{NEW_COST_SHARE_CATEGORIES}::text[]) AS cat
        ON CONFLICT (room_id, booking_type, expense_category) DO NOTHING
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DELETE FROM room_cost_share_rules
        WHERE expense_category IN ('water', 'new_linen_prewash', 'supplies')
          AND share_percent = 1.000 AND unit_price IS NULL
        """
    )
