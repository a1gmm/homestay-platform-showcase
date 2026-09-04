"""Persist retry scheduling for video quarantine cleanup."""

from alembic import op
import sqlalchemy as sa

revision = "miniapp0817c4"
down_revision = "miniapp0817c3"
branch_labels = None
depends_on = None

def upgrade() -> None:
    op.add_column("miniapp_video_uploads", sa.Column("cleanup_attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("miniapp_video_uploads", sa.Column("cleanup_next_attempt_at", sa.DateTime(timezone=True)))
    op.create_index("ix_miniapp_video_uploads_cleanup_due", "miniapp_video_uploads", ["pending_quarantine_cleanup", "cleanup_next_attempt_at"])

def downgrade() -> None:
    op.drop_index("ix_miniapp_video_uploads_cleanup_due", table_name="miniapp_video_uploads")
    op.drop_column("miniapp_video_uploads", "cleanup_next_attempt_at")
    op.drop_column("miniapp_video_uploads", "cleanup_attempt_count")
