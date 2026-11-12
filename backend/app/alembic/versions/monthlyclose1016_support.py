"""Add private monthly-close tutorial feedback, independent of accounting."""
from alembic import op
import sqlalchemy as sa

revision = "monthlyclose1016support"
down_revision = "monthlyclose0915tasks"
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET lock_timeout = '5s'")
    op.create_table(
        "monthly_close_feedback",
        sa.Column("feedback_id", sa.String(32), primary_key=True),
        sa.Column("user_id", sa.String(20), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("request_id", sa.String(36), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("billing_month", sa.String(7), nullable=False),
        sa.Column("step", sa.String(40), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("expected", sa.Text(), nullable=False),
        sa.Column("execution_clicked", sa.String(10), nullable=False),
        sa.Column("screenshot_mime", sa.String(30)),
        sa.Column("screenshot", sa.LargeBinary()),
        sa.Column("status", sa.String(20), nullable=False, server_default="received"),
        sa.Column("reply", sa.Text(), nullable=False, server_default=""),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint("user_id", "request_id", name="uq_mc_feedback_request"),
        sa.CheckConstraint("status IN ('received','processing','resolved')", name="ck_mc_feedback_status"),
    )
    op.create_index("ix_mc_feedback_month_created", "monthly_close_feedback", ["billing_month", "created_at"])


def downgrade():
    op.drop_index("ix_mc_feedback_month_created", table_name="monthly_close_feedback")
    op.drop_table("monthly_close_feedback")
