"""add cleaning_requests table (飞书打扫申请群自助保洁)

Revision ID: a7c3e1f9b2d4
Revises: f7a2c1b4d8e9
Create Date: 2026-07-12

飞书「打扫申请群」自助保洁（PRD 打扫申请群-飞书自助）。保洁自助申请打扫、拿门锁码、
点「打扫完了」撤码；前台/管家点「通过」计费（复用续住打扫 30，房东全担）。
独立于 tasks 表 —— 刻意不复用 cleaning Task，避开退房打扫链路的房态联动。
仅新增一表，不改既有表结构（规避 schema 三真相源 landmine）。
"""
import sqlalchemy as sa
from alembic import op

revision = "a7c3e1f9b2d4"
down_revision = "f7a2c1b4d8e9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "cleaning_requests",
        sa.Column("request_id", sa.String(length=24), primary_key=True),
        sa.Column("room_id", sa.String(length=10), sa.ForeignKey("rooms.room_id"), nullable=False),
        sa.Column("order_id", sa.String(length=20), sa.ForeignKey("orders.order_id"), nullable=False),
        sa.Column("request_date", sa.Date(), nullable=False),
        sa.Column(
            "status",
            sa.Enum("requested", "cleaned", name="cleaning_request_status"),
            nullable=False,
            server_default="requested",
        ),
        sa.Column(
            "approval_status",
            sa.Enum("pending", "approved", "rejected", name="cleaning_approval_status"),
            nullable=False,
            server_default="pending",
        ),
        sa.Column("requester_open_id", sa.String(length=64), nullable=True),
        sa.Column("approver_open_id", sa.String(length=64), nullable=True),
        sa.Column("expense_id", sa.String(length=20), sa.ForeignKey("expenses.expense_id"), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("cleaned_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        # 同房同日只一条打扫申请——DB 级去重键，挡并发/飞书重试双插。
        sa.UniqueConstraint("room_id", "request_date", name="uq_cleaning_requests_room_date"),
    )
    op.create_index("ix_cleaning_requests_order", "cleaning_requests", ["order_id"])


def downgrade() -> None:
    op.drop_index("ix_cleaning_requests_order", table_name="cleaning_requests")
    op.drop_table("cleaning_requests")
    sa.Enum(name="cleaning_approval_status").drop(op.get_bind(), checkfirst=True)
    sa.Enum(name="cleaning_request_status").drop(op.get_bind(), checkfirst=True)
