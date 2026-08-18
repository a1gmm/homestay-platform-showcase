"""door_codes retry backoff: retry_count + next_retry_at

Revision ID: f1a2b3c4d5e6
Revises: v2_7_seed_water_proc
Create Date: 2026-07-22

重推指数退避安全网（连环响事故 2026-07-21）。retry_pending 每 5 分钟一趟，但同一把
未确认码不该每趟都重推——否则「客户端超时但码已进锁」的在线锁会每 5 分钟反复播报
「设置成功」。新增两列：
- retry_count：未确认重推累计次数，据此指数退避（5→10→20→40→60min 封顶）。
- next_retry_at：下次允许重推的时刻；未到即跳过。下成功即清零。
仅新增两列，不改既有结构。
"""
import sqlalchemy as sa
from alembic import op

revision = "f1a2b3c4d5e6"
down_revision = "v2_7_seed_water_proc"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "door_codes",
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "door_codes",
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("door_codes", "next_retry_at")
    op.drop_column("door_codes", "retry_count")
