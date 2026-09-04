"""Fence video quarantine cleanup completion by claim ownership."""

from alembic import op
import sqlalchemy as sa


revision = "miniapp0817c5"
down_revision = "miniapp0817c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "miniapp_video_uploads",
        sa.Column("cleanup_claim_token", sa.String(length=32), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("miniapp_video_uploads", "cleanup_claim_token")
