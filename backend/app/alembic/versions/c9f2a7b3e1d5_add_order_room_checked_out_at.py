"""add order_rooms.checked_out_at for per-room checkout

Revision ID: c9f2a7b3e1d5
Revises: b8d1e4f2a6c7
Create Date: 2026-07-14

单间退房（per-room checkout）：给 order_rooms 加 checked_out_at（nullable，无回填）。
NULL=在住/未退，非空=该房已退房时刻。多房单可逐间退房而不连带整单。
存量行默认 NULL（视为在住），部署后已退房的存量多房单不受影响——它们订单已是
终态/pending_checkout，不会再走单间退房路径。纯加列、可空、无锁风险。
"""
from alembic import op
import sqlalchemy as sa

revision = "c9f2a7b3e1d5"
down_revision = "b8d1e4f2a6c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "order_rooms",
        sa.Column("checked_out_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("order_rooms", "checked_out_at")
