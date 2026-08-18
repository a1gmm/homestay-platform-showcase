"""service_fee_config table + expenses.is_service_fee flag

Revision ID: f7a1c2b3d4e5
Revises: e3c5a9d1f2b4
Create Date: 2026-07-14

新增业主服务费费率配置（单行表 service_fee_config，seed 默认 65/30/15/22）+
给 expenses 加 is_service_fee 布尔列（默认 False，存量行不受影响）。
is_service_fee=True 的费用在业主结算里强制 100% 业主担，绕开占比规则。
纯加表 + 加可空默认列，无锁风险。
"""
from alembic import op
import sqlalchemy as sa


revision = "f7a1c2b3d4e5"
down_revision = "e3c5a9d1f2b4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "expenses",
        sa.Column(
            "is_service_fee",
            sa.Boolean(),
            nullable=False,
            server_default="false",
        ),
    )
    op.create_table(
        "service_fee_config",
        sa.Column("config_id", sa.String(length=20), primary_key=True),
        sa.Column("checkout_cleaning_fee", sa.Numeric(10, 2), nullable=False, server_default="65"),
        sa.Column("instay_cleaning_fee", sa.Numeric(10, 2), nullable=False, server_default="30"),
        sa.Column("laundry_fee_per_room", sa.Numeric(10, 2), nullable=False, server_default="15"),
        sa.Column("consumable_fee_per_room_night", sa.Numeric(10, 2), nullable=False, server_default="22"),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_by", sa.String(length=20), nullable=True),
    )
    # seed 单行默认配置
    op.execute(
        "INSERT INTO service_fee_config "
        "(config_id, checkout_cleaning_fee, instay_cleaning_fee, "
        "laundry_fee_per_room, consumable_fee_per_room_night) "
        "VALUES ('default', 65, 30, 15, 22)"
    )


def downgrade() -> None:
    op.drop_table("service_fee_config")
    op.drop_column("expenses", "is_service_fee")
