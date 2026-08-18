"""multi-room order: order_rooms table + data migration

Revision ID: f4a8c1b3e7d2
Revises: a7c9b2e4f1d8
Create Date: 2026-05-20 14:30:00.000000

新增 order_rooms 子表，把"一单一房"改为"一单多房"。同时把现有 orders 表里的
room_id / check_in_date / check_out_date / list_price / discount_amount /
actual_price 数据 backfill 到 order_rooms 表。

迁移策略：
1. CREATE TABLE order_rooms（结构对应 models/order_room.py）
2. ALTER TABLE owner_settlement_items ADD COLUMN order_room_id（nullable，老数据空）
3. INSERT INTO order_rooms SELECT FROM orders —— 每张订单生成 1 行 order_rooms
4. 校验 count：orders 总数 == order_rooms 总数（一一对应）

orders.room_id / check_in_date / check_out_date / list_price 等列**保留**做读
兼容，6 个月后另写 migration 删除。

迁移后写入路径变化：
- 后端 API 改造完成前（阶段 2-3 之前），新订单仍走旧字段 + 触发器同步到
  order_rooms（不在本 migration 内做）。
- 后端 API 改造完成后，新订单写 order_rooms，orders.room_id 等字段同步留空
  或冗余写入（双写过渡期）。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f4a8c1b3e7d2'
down_revision: Union[str, None] = 'a7c9b2e4f1d8'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1) 建表
    op.create_table(
        "order_rooms",
        sa.Column("order_room_id", sa.String(24), primary_key=True),
        sa.Column("order_id", sa.String(20), sa.ForeignKey("orders.order_id", ondelete="CASCADE"), nullable=False),
        sa.Column("room_id", sa.String(10), sa.ForeignKey("rooms.room_id"), nullable=True),
        sa.Column("check_in_date", sa.Date, nullable=False),
        sa.Column("check_out_date", sa.Date, nullable=False),
        sa.Column("list_price", sa.Numeric(10, 2), nullable=True),
        sa.Column("discount_amount", sa.Numeric(10, 2), nullable=False, server_default="0"),
        sa.Column("actual_price", sa.Numeric(10, 2), nullable=True),
        sa.Column("guests_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("position", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_order_rooms_order", "order_rooms", ["order_id"])
    op.create_index("ix_order_rooms_room_dates", "order_rooms", ["room_id", "check_in_date", "check_out_date"])

    # 2) settlement_items 加 order_room_id 列（老数据为 NULL）
    op.add_column(
        "owner_settlement_items",
        sa.Column("order_room_id", sa.String(24), nullable=True),
    )
    op.create_foreign_key(
        "fk_owner_settlement_items_order_room",
        "owner_settlement_items",
        "order_rooms",
        ["order_room_id"],
        ["order_room_id"],
    )

    # 3) 数据迁移：每张已有 orders → 1 行 order_rooms。
    # order_room_id = 'OR-' + UPPER(SUBSTRING(MD5(order_id) FROM 1 FOR 16))，确定性 + 幂等。
    op.execute(
        """
        INSERT INTO order_rooms (
            order_room_id, order_id, room_id,
            check_in_date, check_out_date,
            list_price, discount_amount, actual_price,
            guests_count, position, created_at, updated_at
        )
        SELECT
            'OR-' || UPPER(SUBSTRING(MD5(o.order_id) FROM 1 FOR 16)),
            o.order_id,
            o.room_id,
            o.check_in_date,
            o.check_out_date,
            o.list_price,
            COALESCE(o.discount_amount, 0),
            o.actual_price,
            0,
            0,
            o.created_at,
            o.updated_at
        FROM orders o
        WHERE NOT EXISTS (
            SELECT 1 FROM order_rooms r WHERE r.order_id = o.order_id
        )
        """
    )

    # 4) 校验：迁移后 orders 总数应等于 order_rooms 总数
    conn = op.get_bind()
    orders_count = conn.execute(sa.text("SELECT COUNT(*) FROM orders")).scalar() or 0
    order_rooms_count = conn.execute(sa.text("SELECT COUNT(*) FROM order_rooms")).scalar() or 0
    if orders_count != order_rooms_count:
        raise RuntimeError(
            f"order_rooms 数据迁移行数不匹配：orders={orders_count}, order_rooms={order_rooms_count}"
        )


def downgrade() -> None:
    op.drop_constraint("fk_owner_settlement_items_order_room", "owner_settlement_items", type_="foreignkey")
    op.drop_column("owner_settlement_items", "order_room_id")
    op.drop_index("ix_order_rooms_room_dates", table_name="order_rooms")
    op.drop_index("ix_order_rooms_order", table_name="order_rooms")
    op.drop_table("order_rooms")
