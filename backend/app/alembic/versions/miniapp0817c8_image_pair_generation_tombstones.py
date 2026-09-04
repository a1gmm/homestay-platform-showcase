"""Keep exact image-pair generation keys until late writes are reconciled."""

from alembic import op
import sqlalchemy as sa


revision = "miniapp0817c8"
down_revision = "miniapp0817c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint(
        "ck_miniapp_image_pair_uploads_status",
        "miniapp_image_pair_uploads",
        type_="check",
    )
    op.create_check_constraint(
        "ck_miniapp_image_pair_uploads_status",
        "miniapp_image_pair_uploads",
        "status IN ('reserved', 'cleanup_pending', 'cleaning', 'retired', 'finalized')",
    )
    op.create_table(
        "miniapp_image_pair_generations",
        sa.Column("derivative_set_id", sa.String(24), nullable=False),
        sa.Column("key_generation", sa.Integer(), nullable=False),
        sa.Column("thumbnail_key", sa.String(500), nullable=False, unique=True),
        sa.Column("display_key", sa.String(500), nullable=False, unique=True),
        sa.Column("status", sa.String(20), nullable=False, server_default="active"),
        sa.Column("cleanup_attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("next_probe_at", sa.DateTime(timezone=True)),
        sa.Column("retire_at", sa.DateTime(timezone=True)),
        sa.Column("cleanup_claim_token", sa.String(32)),
        sa.Column("error", sa.String(240)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.ForeignKeyConstraint(
            ["derivative_set_id"],
            ["miniapp_image_pair_uploads.derivative_set_id"],
            name="fk_miniapp_image_pair_generations_upload",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint(
            "derivative_set_id",
            "key_generation",
            name="pk_miniapp_image_pair_generations",
        ),
        sa.CheckConstraint(
            "status IN ('active', 'cleanup_pending', 'cleaning', 'retired', 'finalized')",
            name="ck_miniapp_image_pair_generations_status",
        ),
        sa.CheckConstraint(
            "key_generation >= 1",
            name="ck_miniapp_image_pair_generations_key_generation",
        ),
        sa.CheckConstraint(
            "cleanup_attempt_count >= 0",
            name="ck_miniapp_image_pair_generations_cleanup_attempts",
        ),
        sa.CheckConstraint(
            "thumbnail_key <> display_key",
            name="ck_miniapp_image_pair_generations_keys_distinct",
        ),
    )
    op.create_index(
        "ix_miniapp_image_pair_generations_cleanup_due",
        "miniapp_image_pair_generations",
        ["status", "next_probe_at"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO miniapp_image_pair_generations (
                derivative_set_id, key_generation, thumbnail_key, display_key,
                status, cleanup_attempt_count, next_probe_at, retire_at,
                cleanup_claim_token, error, created_at, updated_at
            )
            SELECT
                derivative_set_id, key_generation, thumbnail_key, display_key,
                CASE
                    WHEN status = 'finalized' THEN 'finalized'
                    WHEN status = 'reserved' THEN 'active'
                    ELSE status
                END,
                cleanup_attempt_count,
                cleanup_next_attempt_at,
                CASE
                    WHEN status IN ('cleanup_pending', 'cleaning')
                    THEN COALESCE(cleanup_next_attempt_at, updated_at) + INTERVAL '30 minutes'
                    ELSE NULL
                END,
                cleanup_claim_token, error, created_at, updated_at
            FROM miniapp_image_pair_uploads
            """
        )
    )


def downgrade() -> None:
    op.drop_index(
        "ix_miniapp_image_pair_generations_cleanup_due",
        table_name="miniapp_image_pair_generations",
    )
    op.drop_table("miniapp_image_pair_generations")
    op.execute(
        sa.text(
            "UPDATE miniapp_image_pair_uploads SET status = 'reserved' "
            "WHERE status = 'retired'"
        )
    )
    op.drop_constraint(
        "ck_miniapp_image_pair_uploads_status",
        "miniapp_image_pair_uploads",
        type_="check",
    )
    op.create_check_constraint(
        "ck_miniapp_image_pair_uploads_status",
        "miniapp_image_pair_uploads",
        "status IN ('reserved', 'cleanup_pending', 'cleaning', 'finalized')",
    )
