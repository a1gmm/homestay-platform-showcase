"""Persist mini-program video upload finalization state."""

from alembic import op
import sqlalchemy as sa


revision = "miniapp0817c2"
down_revision = "miniapp0816c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "miniapp_video_uploads",
        sa.Column("media_id", sa.String(24), primary_key=True),
        sa.Column("quarantine_key", sa.String(500), nullable=False, unique=True),
        sa.Column("draft_key", sa.String(500), nullable=False, unique=True),
        sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("declared_size", sa.Integer(), nullable=False),
        sa.Column("expected_sha256", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="uploading"),
        sa.Column("mime_type", sa.String(100)),
        sa.Column("error", sa.String(240)),
        sa.Column("pending_draft_cleanup", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("status IN ('uploading', 'finalizing', 'finalized', 'failed')", name="ck_miniapp_video_uploads_status"),
        sa.CheckConstraint("declared_size > 0", name="ck_miniapp_video_uploads_size"),
    )


def downgrade() -> None:
    op.drop_table("miniapp_video_uploads")
