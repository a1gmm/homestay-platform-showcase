"""add smart-lock tables: lock_devices, door_codes

Revision ID: 5c0b1a2d3e4f
Revises: c8d9e0f1a2b3
Create Date: 2026-06-13

智能门锁 Phase 1 Lane B（见 plan §8.2 / §16）。仅新增两张表，不改动任何既有表/约束
（规避 schema 三套真相源 landmine）。door_codes 的 partial unique index 保证同一
(order_id, room_id, purpose) 在 active/pending 态只能一把 → 幂等防重复下码。

注意：partial index / ENUM 是 Postgres 特性，本迁移不在 SQLite 测试库执行；
模型层等价约束由 tests/test_lock_models.py 在 SQLite 上覆盖。上线前在 Neon staging dry-run。
"""
import sqlalchemy as sa
from alembic import op

revision = "5c0b1a2d3e4f"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None

_ACTIVE_STATES = "status IN ('pending', 'active')"


def upgrade() -> None:
    op.create_table(
        "lock_devices",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("room_id", sa.String(length=10), sa.ForeignKey("rooms.room_id"), nullable=False),
        sa.Column("vendor_name", sa.String(length=32), nullable=False, server_default="hxjiot"),
        sa.Column("vendor_room_id", sa.String(length=64), nullable=False),
        sa.Column("vendor_lock_mac", sa.String(length=32), nullable=True),
        sa.Column("last_online_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_battery", sa.SmallInteger(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("vendor_name", "vendor_room_id", name="uq_lock_devices_vendor_room"),
    )

    purpose = sa.Enum("guest", "cleaning", "keeper", "master", name="door_code_purpose")
    status = sa.Enum(
        "pending", "active", "revoked", "expired", "failed", "manual", name="door_code_status"
    )

    op.create_table(
        "door_codes",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("order_id", sa.String(length=32), sa.ForeignKey("orders.order_id"), nullable=True),
        sa.Column("room_id", sa.String(length=10), sa.ForeignKey("rooms.room_id"), nullable=False),
        sa.Column("lock_device_id", sa.Integer(), sa.ForeignKey("lock_devices.id"), nullable=True),
        sa.Column("purpose", purpose, nullable=False),
        sa.Column("phone_no", sa.String(length=20), nullable=True),
        sa.Column("password", sa.String(length=255), nullable=True),
        sa.Column("status", status, nullable=False),
        sa.Column("vendor_key_id", sa.String(length=64), nullable=True),
        sa.Column("vendor_lock_key_id", sa.Integer(), nullable=True),
        sa.Column("vendor_key_group_id", sa.Integer(), nullable=True),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
    )

    # 幂等：仅对 active/pending 态去重（partial unique index，Postgres）
    op.create_index(
        "uq_door_codes_active_per_order_room_purpose",
        "door_codes",
        ["order_id", "room_id", "purpose"],
        unique=True,
        postgresql_where=sa.text(_ACTIVE_STATES),
    )
    op.create_index(
        "ix_door_codes_room_status_start", "door_codes", ["room_id", "status", "start_at"]
    )
    op.create_index("ix_door_codes_order_purpose", "door_codes", ["order_id", "purpose"])


def downgrade() -> None:
    op.drop_index("ix_door_codes_order_purpose", table_name="door_codes")
    op.drop_index("ix_door_codes_room_status_start", table_name="door_codes")
    op.drop_index("uq_door_codes_active_per_order_room_purpose", table_name="door_codes")
    op.drop_table("door_codes")
    op.drop_table("lock_devices")
    op.execute("DROP TYPE IF EXISTS door_code_status")
    op.execute("DROP TYPE IF EXISTS door_code_purpose")
