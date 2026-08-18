"""v2#2 RoomCostShareRule table

Revision ID: db1e9fb8cd5d
Revises: 3fef7e5887be
Create Date: 2026-05-09 23:46:43.793043

新建 room_cost_share_rules 表，存「房间 × 订单类型 × 费用类目」三维配置。

booking_type / expense_category enum 已在前序 migration 创建（dc4493008dc2 + 3fef7e5887be），
本 migration 仅建表，引用现有 enum type（postgresql_existing=True 风格）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = 'db1e9fb8cd5d'
down_revision: Union[str, None] = '3fef7e5887be'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "room_cost_share_rules",
        sa.Column("rule_id", sa.String(20), primary_key=True),
        sa.Column(
            "room_id",
            sa.String(10),
            sa.ForeignKey("rooms.room_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # booking_type enum 已存在，create_type=False 避免重复 CREATE TYPE
        sa.Column(
            "booking_type",
            postgresql.ENUM(
                "normal", "trial", "owner_self",
                name="booking_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "expense_category",
            postgresql.ENUM(
                "cleaning", "maintenance", "utilities", "supplies",
                "platform_fee", "tax", "other",
                "public_utilities", "cold_water", "broadband", "daily_supplies",
                "laundry", "hot_water", "gas", "property_fee", "electricity",
                "kitchen_cleaning", "property_guidance_fee",
                name="expense_category",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "share_percent",
            sa.Numeric(5, 4),
            nullable=False,
            server_default="1.0000",
        ),
        sa.Column("unit_price", sa.Numeric(10, 2), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "room_id", "booking_type", "expense_category",
            name="uq_room_cost_share_room_booking_category",
        ),
    )
    op.create_index(
        "ix_room_cost_share_room_booking",
        "room_cost_share_rules",
        ["room_id", "booking_type"],
    )


def downgrade() -> None:
    op.drop_index("ix_room_cost_share_room_booking", table_name="room_cost_share_rules")
    op.drop_table("room_cost_share_rules")
