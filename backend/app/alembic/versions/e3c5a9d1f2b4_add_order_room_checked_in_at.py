"""add order_rooms.checked_in_at for per-room checkin

Revision ID: e3c5a9d1f2b4
Revises: d2b4f6a8c1e3
Create Date: 2026-07-14

单间入住（per-room checkin，镜像 checked_out_at）：给 order_rooms 加 checked_in_at（nullable，无回填）。
NULL=未入住，非空=该房已入住时刻。多房单可逐间办理入住而不连带整单。
存量行默认 NULL——已在住/已退房的存量单不受影响（它们已过入住阶段，不会再走单间入住路径；
展示层遇 NULL 时按订单 order_status 兜底，见 schemas/order.py 说明）。纯加列、可空、无锁风险。
"""
from alembic import op
import sqlalchemy as sa

revision = "e3c5a9d1f2b4"
down_revision = "d2b4f6a8c1e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "order_rooms",
        sa.Column("checked_in_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("order_rooms", "checked_in_at")
