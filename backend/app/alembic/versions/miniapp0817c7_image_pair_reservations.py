"""Reserve image derivative sets before object storage writes."""

from alembic import op
import sqlalchemy as sa


revision = "miniapp0817c7"
down_revision = "miniapp0817c6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "miniapp_image_pair_uploads",
        sa.Column("derivative_set_id", sa.String(24), primary_key=True),
        sa.Column("source_sha256", sa.String(64), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="reserved"),
        sa.Column("key_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("thumbnail_media_id", sa.String(24), nullable=False, unique=True),
        sa.Column("display_media_id", sa.String(24), nullable=False, unique=True),
        sa.Column("thumbnail_key", sa.String(500), nullable=False, unique=True),
        sa.Column("display_key", sa.String(500), nullable=False, unique=True),
        sa.Column("thumbnail_sha256", sa.String(64), nullable=False),
        sa.Column("display_sha256", sa.String(64), nullable=False),
        sa.Column("thumbnail_size", sa.Integer(), nullable=False),
        sa.Column("display_size", sa.Integer(), nullable=False),
        sa.Column("thumbnail_width", sa.Integer(), nullable=False),
        sa.Column("thumbnail_height", sa.Integer(), nullable=False),
        sa.Column("display_width", sa.Integer(), nullable=False),
        sa.Column("display_height", sa.Integer(), nullable=False),
        sa.Column("cleanup_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("cleanup_next_attempt_at", sa.DateTime(timezone=True)),
        sa.Column("cleanup_claim_token", sa.String(32)),
        sa.Column("error", sa.String(240)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('reserved', 'cleanup_pending', 'cleaning', 'finalized')",
            name="ck_miniapp_image_pair_uploads_status",
        ),
        sa.CheckConstraint("key_generation >= 1", name="ck_miniapp_image_pair_uploads_key_generation"),
        sa.CheckConstraint(
            "length(source_sha256) = 64 AND length(thumbnail_sha256) = 64 "
            "AND length(display_sha256) = 64",
            name="ck_miniapp_image_pair_uploads_digest_lengths",
        ),
        sa.CheckConstraint(
            "cleanup_attempt_count >= 0",
            name="ck_miniapp_image_pair_uploads_cleanup_attempts",
        ),
        sa.CheckConstraint("thumbnail_media_id <> display_media_id", name="ck_miniapp_image_pair_uploads_media_ids_distinct"),
        sa.CheckConstraint("thumbnail_key <> display_key", name="ck_miniapp_image_pair_uploads_keys_distinct"),
        sa.CheckConstraint("thumbnail_sha256 <> display_sha256", name="ck_miniapp_image_pair_uploads_hashes_distinct"),
        sa.CheckConstraint(
            "thumbnail_size > 0 AND display_size > 0 "
            "AND thumbnail_width > 0 AND thumbnail_height > 0 "
            "AND display_width > 0 AND display_height > 0",
            name="ck_miniapp_image_pair_uploads_positive_media",
        ),
    )
    op.create_index(
        "ix_miniapp_image_pair_uploads_cleanup_due",
        "miniapp_image_pair_uploads",
        ["status", "cleanup_next_attempt_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_miniapp_image_pair_uploads_cleanup_due",
        table_name="miniapp_image_pair_uploads",
    )
    op.drop_table("miniapp_image_pair_uploads")
