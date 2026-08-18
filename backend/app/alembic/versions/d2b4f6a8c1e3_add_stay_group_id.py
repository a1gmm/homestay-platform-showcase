"""add stay_group_id to orders (续住关联组号)

Revision ID: d2b4f6a8c1e3
Revises: c9f2a7b3e1d5
Create Date: 2026-07-14
"""
from alembic import op
import sqlalchemy as sa

revision = "d2b4f6a8c1e3"
down_revision = "c9f2a7b3e1d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("orders", sa.Column("stay_group_id", sa.String(length=32), nullable=True))
    op.create_index("ix_orders_stay_group", "orders", ["stay_group_id"])


def downgrade() -> None:
    op.drop_index("ix_orders_stay_group", table_name="orders")
    op.drop_column("orders", "stay_group_id")
