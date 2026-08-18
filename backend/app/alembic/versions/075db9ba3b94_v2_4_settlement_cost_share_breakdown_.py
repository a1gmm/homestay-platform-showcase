"""v2#4 settlement cost_share_breakdown column

Revision ID: 075db9ba3b94
Revises: db1e9fb8cd5d
Create Date: 2026-05-09 23:54:41.749987

为 owner_settlement_items 加 cost_share_breakdown JSONB 列，存每笔费用按
RoomCostShareRule 加权后的明细，供业主端展示费用拆解（v2-issue#5）。

老结算单数据该列默认 '[]'（空数组）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '075db9ba3b94'
down_revision: Union[str, None] = 'db1e9fb8cd5d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "owner_settlement_items",
        sa.Column(
            "cost_share_breakdown",
            postgresql.JSONB,
            nullable=False,
            server_default="[]",
        ),
    )


def downgrade() -> None:
    op.drop_column("owner_settlement_items", "cost_share_breakdown")
