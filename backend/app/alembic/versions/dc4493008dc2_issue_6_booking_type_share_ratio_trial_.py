"""issue#6 booking_type + share_ratio_trial/owner_self

Revision ID: dc4493008dc2
Revises: 2735db4afb5b
Create Date: 2026-05-09 22:35:56.171056

王总反馈："小程序里会分三个模块组成，普通订单，自住单，试住单。后台需要把
这三大模块的分成比例设定好"。

本 migration 做两件事：
1. orders 表加 booking_type 列（enum: normal/trial/owner_self），默认 normal
2. rooms 表加 share_ratio_trial / share_ratio_owner_self 两列（默认 1.0 = 业主全担）
   保留 owner_share_ratio（=普通单分成）字段不动，避免破坏 30+ 处现有引用

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'dc4493008dc2'
down_revision: Union[str, None] = '2735db4afb5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # 1. Order.booking_type — 创建 enum + 加列
    booking_type_enum = sa.Enum(
        "normal", "trial", "owner_self", name="booking_type"
    )
    booking_type_enum.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "orders",
        sa.Column(
            "booking_type",
            booking_type_enum,
            nullable=False,
            server_default="normal",
        ),
    )

    # 2. Room 加两列分成比例。默认 1.0 = 业主全担（保守值，王总后续可在
    #    "分成比例配置后台" issue#7 里逐房间调整）
    op.add_column(
        "rooms",
        sa.Column(
            "share_ratio_trial",
            sa.Numeric(4, 3),
            nullable=False,
            server_default="1.000",
        ),
    )
    op.add_column(
        "rooms",
        sa.Column(
            "share_ratio_owner_self",
            sa.Numeric(4, 3),
            nullable=False,
            server_default="1.000",
        ),
    )


def downgrade() -> None:
    op.drop_column("rooms", "share_ratio_owner_self")
    op.drop_column("rooms", "share_ratio_trial")
    op.drop_column("orders", "booking_type")
    sa.Enum(name="booking_type").drop(op.get_bind(), checkfirst=True)
