"""Persist resumable administrator monthly-close goals."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "monthlyclose0915tasks"
down_revision = "settlement0910details"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET lock_timeout = '5s'")
    op.create_table(
        "monthly_close_tasks",
        sa.Column("task_id", sa.String(32), primary_key=True),
        sa.Column("cycle_id", sa.String(24), sa.ForeignKey("monthly_close_cycles.cycle_id"), nullable=False),
        sa.Column("actor_id", sa.String(20), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("goal", sa.Text(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("retry_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("lease_token", sa.String(32)),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True)),
        sa.Column("next_run_at", sa.DateTime(timezone=True)),
        sa.Column("checkpoint", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("result", postgresql.JSONB(), nullable=False, server_default="{}"),
        sa.Column("events", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column("last_safe_error", sa.String(250)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("cycle_id", "actor_id", name="uq_monthly_close_tasks_cycle_actor"),
        sa.CheckConstraint("status IN ('queued','running','waiting_user','waiting_approval','paused','succeeded','failed')", name="ck_monthly_close_tasks_status"),
    )
    op.create_index("ix_monthly_close_tasks_dispatch", "monthly_close_tasks", ["status", "next_run_at"])


def downgrade():
    op.drop_index("ix_monthly_close_tasks_dispatch", table_name="monthly_close_tasks")
    op.drop_table("monthly_close_tasks")
