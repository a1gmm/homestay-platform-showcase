"""order_rooms 加 metadata JSONB（房间转移溯源）

Revision ID: a1b2c3d4e5f6
Revises: c8d9e0f1a2b3
Create Date: 2026-06-13 00:00:00.000000

换房（房间转移）功能：换房原因/来源存 OrderRoom.metadata_。order_rooms 原本无
metadata 列，加之。前端可据此在房间行标「本单因换房拆分」。
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = 'c8d9e0f1a2b3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "order_rooms",
        sa.Column("metadata", postgresql.JSONB(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("order_rooms", "metadata")
