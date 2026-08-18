"""owner_settlement_items 支持业主级支出行（room_id 可空 + label）

Revision ID: f8b3d5a1c9e2
Revises: f7a1c2b3d4e5
Create Date: 2026-07-18

方案 B（王总 2026-07-18）：整层/某业主电费等「业主级支出」存一条挂 owner_id、
room_id 为空，结算时整笔从业主分成扣。结算明细表需要能落这种无房间的行：
- room_id 放开成可空（原 NOT NULL）——仅放松约束，存量逐房行不受影响。
- 新增 label 可空列，业主级行用它显示「电费」等类目名，逐房行为空回落房名。
纯放松约束 + 加可空列，无数据变更、无锁风险。
"""
from alembic import op
import sqlalchemy as sa


revision = "f8b3d5a1c9e2"
down_revision = "f7a1c2b3d4e5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "owner_settlement_items",
        "room_id",
        existing_type=sa.String(length=10),
        nullable=True,
    )
    op.add_column(
        "owner_settlement_items",
        sa.Column("label", sa.String(length=50), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("owner_settlement_items", "label")
    # 回滚前需确保没有 room_id 为空的行，否则 NOT NULL 会失败。
    op.alter_column(
        "owner_settlement_items",
        "room_id",
        existing_type=sa.String(length=10),
        nullable=False,
    )
