"""Close final owner-content media and draft durability gaps."""

from alembic import op
import sqlalchemy as sa


revision = "miniapp0822c1"
down_revision = "miniapp0817c8"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "miniapp_media",
        sa.Column(
            "pending_private_cleanup",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "miniapp_media",
        sa.Column("cleanup_attempt_count", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "miniapp_media",
        sa.Column("cleanup_next_attempt_at", sa.DateTime(timezone=True)),
    )
    op.add_column(
        "miniapp_media", sa.Column("cleanup_claim_token", sa.String(32))
    )
    op.add_column(
        "miniapp_media", sa.Column("cleanup_error", sa.String(240))
    )
    op.create_index(
        "ix_miniapp_media_private_cleanup_due",
        "miniapp_media",
        ["pending_private_cleanup", "cleanup_next_attempt_at"],
    )

    op.add_column("miniapp_video_uploads", sa.Column("width", sa.Integer()))
    op.add_column("miniapp_video_uploads", sa.Column("height", sa.Integer()))
    op.create_check_constraint(
        "ck_miniapp_video_uploads_dimensions",
        "miniapp_video_uploads",
        "(width IS NULL AND height IS NULL) OR (width > 0 AND height > 0)",
    )

    op.create_table(
        "owner_draft_videos",
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("video_media_id", sa.String(24), nullable=False),
        sa.Column("poster_media_id", sa.String(24), nullable=False),
        sa.Column("alt", sa.String(200), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["channel"],
            ["miniapp_content_workspaces.channel"],
            name="fk_owner_draft_videos_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["video_media_id"],
            ["miniapp_media.media_id"],
            name="fk_owner_draft_videos_video_media",
        ),
        sa.ForeignKeyConstraint(
            ["poster_media_id"],
            ["miniapp_media.media_id"],
            name="fk_owner_draft_videos_poster_media",
        ),
        sa.PrimaryKeyConstraint("channel", name="pk_owner_draft_videos"),
        sa.CheckConstraint(
            "channel = 'owner'", name="ck_owner_draft_videos_channel"
        ),
        sa.CheckConstraint(
            "revision >= 1", name="ck_owner_draft_videos_revision"
        ),
        sa.CheckConstraint(
            "video_media_id <> poster_media_id",
            name="ck_owner_draft_videos_distinct_media",
        ),
    )


def downgrade() -> None:
    op.drop_table("owner_draft_videos")
    op.drop_constraint(
        "ck_miniapp_video_uploads_dimensions",
        "miniapp_video_uploads",
        type_="check",
    )
    op.drop_column("miniapp_video_uploads", "height")
    op.drop_column("miniapp_video_uploads", "width")
    op.drop_index(
        "ix_miniapp_media_private_cleanup_due", table_name="miniapp_media"
    )
    op.drop_column("miniapp_media", "cleanup_error")
    op.drop_column("miniapp_media", "cleanup_claim_token")
    op.drop_column("miniapp_media", "cleanup_next_attempt_at")
    op.drop_column("miniapp_media", "cleanup_attempt_count")
    op.drop_column("miniapp_media", "pending_private_cleanup")
