"""add lock_events table + lock_devices.battery_alerted

Revision ID: f7a2c1b4d8e9
Revises: e6031ed97daf
Create Date: 2026-07-11

门锁状态自动同步（PLAN_lock_auto_sync 方案1）。慧享家 lockLogPush 开锁推送落库：
- lock_events：一条 push 一行，log_id 唯一去重；带 electric_num 供电量自愈。
- lock_devices.battery_alerted：低电告警滞回闸，防刷屏。
仅新增一表 + 一列，不改既有表结构（规避 schema 三真相源 landmine）。
"""
import sqlalchemy as sa
from alembic import op

revision = "f7a2c1b4d8e9"
down_revision = "e6031ed97daf"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "lock_events",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("log_id", sa.String(length=64), nullable=False),
        sa.Column("vendor_room_id", sa.String(length=64), nullable=True),
        sa.Column("lock_mac", sa.String(length=32), nullable=True),
        sa.Column("log_type", sa.Integer(), nullable=True),
        sa.Column("log_level", sa.String(length=16), nullable=True),
        sa.Column("log_alert", sa.Text(), nullable=True),
        sa.Column("electric_num", sa.SmallInteger(), nullable=True),
        sa.Column("photo_url", sa.Text(), nullable=True),
        sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("log_id", name="uq_lock_events_log_id"),
    )
    op.add_column(
        "lock_devices",
        sa.Column("battery_alerted", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("lock_devices", "battery_alerted")
    op.drop_table("lock_events")
