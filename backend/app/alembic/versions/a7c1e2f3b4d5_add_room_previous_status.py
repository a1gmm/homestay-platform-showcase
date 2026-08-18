"""rooms add previous_status (维修/锁房一键恢复)

Revision ID: a7c1e2f3b4d5
Revises: f5b9c2d7e3a1
Create Date: 2026-06-06 00:00:00.000000

房间进入维修/锁房状态后，结束时需要"一键恢复上一个状态"。本迁移给 rooms 加
previous_status 列（可空），复用已存在的 room_status PG enum 类型（create_type=False，
不重复建类型）。每次 room_status 变化时由 PATCH /rooms/{id} 记录旧值。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a7c1e2f3b4d5'
down_revision: Union[str, None] = 'f5b9c2d7e3a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "rooms",
        sa.Column(
            "previous_status",
            postgresql.ENUM(name="room_status", create_type=False),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("rooms", "previous_status")
