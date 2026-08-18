"""账单对账两表 recon_batches / recon_diffs

Revision ID: billrecon0802
Revises: a9f1_owner_hide_amounts
Create Date: 2026-08-02

纯加表加枚举，无存量数据变更、无锁风险。
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "billrecon0802"
down_revision = "a9f1_owner_hide_amounts"
branch_labels = None
depends_on = None

diff_class = sa.Enum(
    "fix_amount", "appeal", "broken_link", "compensation", "manual_review",
    name="recon_diff_class",
)
diff_status = sa.Enum(
    "pending", "adopted", "already_consistent", "dismissed",
    "appeal_pending", "appeal_settled", "acknowledged",
    name="recon_diff_status",
)


def upgrade() -> None:
    # created_by FK 指向存量 users 表，验证需拿锁；撞上僵尸锁时 5s 快失败，
    # 别让 `alembic upgrade head && uvicorn` 无限挂起卡死整次部署（同 main.py lifespan 的守卫口径）
    op.execute("SET lock_timeout = '5s'")
    op.create_table(
        "recon_batches",
        sa.Column("batch_id", sa.String(40), primary_key=True),
        sa.Column("platform", sa.String(20), nullable=False, server_default="ctrip"),
        sa.Column("bill_month", sa.String(7), nullable=False),
        sa.Column("summary_total", sa.Numeric(12, 2), nullable=False, server_default="0"),
        sa.Column("row_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("status", sa.String(20), nullable=False, server_default="parsed"),
        sa.Column("error", sa.Text),
        sa.Column("mapping", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("created_by", sa.String(20), sa.ForeignKey("users.user_id")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_recon_batches_month", "recon_batches", ["platform", "bill_month"])
    op.create_table(
        "recon_diffs",
        sa.Column("diff_id", sa.String(40), primary_key=True),
        sa.Column("batch_id", sa.String(40), sa.ForeignKey("recon_batches.batch_id"), nullable=False),
        sa.Column("order_id", sa.String(20)),
        sa.Column("platform_order_id", sa.String(100)),
        sa.Column("guest_name", sa.String(50)),
        sa.Column("diff_class", diff_class, nullable=False),
        sa.Column("status", diff_status, nullable=False, server_default="pending"),
        sa.Column("bill_amount", sa.Numeric(10, 2)),
        sa.Column("system_amount", sa.Numeric(10, 2)),
        sa.Column("detail", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("resolved_by", sa.String(20)),
        sa.Column("resolved_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
    )
    op.create_index("ix_recon_diffs_batch", "recon_diffs", ["batch_id"])
    op.create_index("ix_recon_diffs_status", "recon_diffs", ["status"])


def downgrade() -> None:
    op.drop_table("recon_diffs")
    op.drop_table("recon_batches")
    diff_status.drop(op.get_bind(), checkfirst=True)
    diff_class.drop(op.get_bind(), checkfirst=True)
