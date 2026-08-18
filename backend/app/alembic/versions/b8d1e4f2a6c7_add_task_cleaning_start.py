"""add task cleaning start-tracking columns

Revision ID: b8d1e4f2a6c7
Revises: a7c3e1f9b2d4
Create Date: 2026-07-14

退房打扫两步自助 + 串行门闩：给 tasks 加「谁开始打扫、何时开始」两列（均 nullable，
无回填、无锁风险）。门闩查询 = 该 open_id 名下有无「已开始未完成」的清扫任务。
"""
from alembic import op
import sqlalchemy as sa

revision = "b8d1e4f2a6c7"
down_revision = "a7c3e1f9b2d4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tasks", sa.Column("cleaning_started_by", sa.String(length=64), nullable=True))
    op.add_column("tasks", sa.Column("cleaning_started_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("tasks", "cleaning_started_at")
    op.drop_column("tasks", "cleaning_started_by")
