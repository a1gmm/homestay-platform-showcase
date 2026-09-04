"""Make quarantine cleanup a durable post-finalize action."""

from alembic import op
import sqlalchemy as sa


revision = "miniapp0817c3"
down_revision = "miniapp0817c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "miniapp_video_uploads",
        sa.Column("pending_quarantine_cleanup", sa.Boolean(), nullable=False, server_default="false"),
    )


def downgrade() -> None:
    op.drop_column("miniapp_video_uploads", "pending_quarantine_cleanup")
