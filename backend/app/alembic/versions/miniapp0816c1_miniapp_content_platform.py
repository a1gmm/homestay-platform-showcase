"""miniapp content platform owner-channel database skeleton

Revision ID: miniapp0816c1
Revises: billrecon0802
Create Date: 2026-08-16

Creates only the seven Task 1 content-platform tables. The frozen billrecon0802
bootstrap snapshot remains unchanged; fresh databases install that snapshot and
then execute this descendant migration normally.
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "miniapp0816c1"
down_revision = "billrecon0802"
branch_labels = None
depends_on = None

CHANNEL_CHECK = "channel IN ('owner', 'stay_guide', 'travel')"


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")

    op.create_table(
        "miniapp_content_releases",
        sa.Column("release_id", sa.String(24), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("version", sa.String(40), nullable=False),
        sa.Column("manifest_url", sa.String(500), nullable=False),
        sa.Column("manifest_sha256", sa.String(64), nullable=False),
        sa.Column("manifest_payload", postgresql.JSONB()),
        sa.Column("published_at", sa.DateTime(timezone=True)),
        sa.Column("pointer_swapped_at", sa.DateTime(timezone=True)),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("created_by", sa.String(20), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(CHANNEL_CHECK, name="ck_miniapp_content_releases_channel"),
        sa.CheckConstraint(
            "status IN ('pending', 'promoting', 'published', 'failed', 'superseded', 'rolled_back')",
            name="ck_miniapp_content_releases_status",
        ),
        sa.UniqueConstraint(
            "channel", "version", name="uq_miniapp_content_releases_channel_version"
        ),
    )
    op.create_index(
        "ix_miniapp_content_releases_channel",
        "miniapp_content_releases",
        ["channel"],
    )

    op.create_table(
        "miniapp_content_workspaces",
        sa.Column("channel", sa.String(20), primary_key=True),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("is_dirty", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column(
            "current_release_id",
            sa.String(24),
            sa.ForeignKey("miniapp_content_releases.release_id"),
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(CHANNEL_CHECK, name="ck_miniapp_content_workspaces_channel"),
        sa.CheckConstraint("revision >= 0", name="ck_miniapp_content_workspaces_revision"),
    )

    op.create_table(
        "miniapp_channel_configs",
        sa.Column("channel", sa.String(20), primary_key=True),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(CHANNEL_CHECK, name="ck_miniapp_channel_configs_channel"),
    )

    op.create_table(
        "miniapp_media",
        sa.Column("media_id", sa.String(24), primary_key=True),
        sa.Column("object_key", sa.String(500), nullable=False, unique=True),
        sa.Column("media_type", sa.String(20), nullable=False),
        sa.Column("mime_type", sa.String(100), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("reference_status", sa.String(20), nullable=False, server_default="draft"),
        sa.Column("last_referenced_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("media_type IN ('image', 'video')", name="ck_miniapp_media_type"),
        sa.CheckConstraint("size_bytes >= 0", name="ck_miniapp_media_size_bytes"),
        sa.CheckConstraint(
            "reference_status IN ('draft', 'published', 'unreferenced')",
            name="ck_miniapp_media_reference_status",
        ),
    )

    op.create_table(
        "owner_cases",
        sa.Column("case_id", sa.String(24), primary_key=True),
        sa.Column("title", sa.String(120), nullable=False),
        sa.Column("description", sa.Text()),
        sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("revision", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("is_visible", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("primary_media_id", sa.String(24), sa.ForeignKey("miniapp_media.media_id")),
        sa.Column(
            "detail_media_ids",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        sa.Column("privacy_confirmed_by", sa.String(20), sa.ForeignKey("users.user_id")),
        sa.Column("privacy_confirmed_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint("revision >= 1", name="ck_owner_cases_revision"),
        sa.CheckConstraint(
            "NOT is_visible OR privacy_confirmed_by IS NOT NULL",
            name="ck_owner_cases_visible_privacy_confirmed",
        ),
    )

    op.create_table(
        "miniapp_publish_jobs",
        sa.Column("job_id", sa.String(24), primary_key=True),
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("operation", sa.String(20), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="queued"),
        sa.Column("expected_revision", sa.Integer()),
        sa.Column("target_version", sa.String(40)),
        sa.Column(
            "result_release_id",
            sa.String(24),
            sa.ForeignKey("miniapp_content_releases.release_id"),
        ),
        sa.Column(
            "previous_release_id",
            sa.String(24),
            sa.ForeignKey("miniapp_content_releases.release_id"),
        ),
        sa.Column("actor_id", sa.String(20), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("idempotency_scope", sa.String(80), nullable=False, server_default="miniapp_content"),
        sa.Column("idempotency_key", sa.String(120), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("attempt_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "attempt_token",
            sa.String(32),
            nullable=False,
            server_default="00000000000000000000000000000000",
        ),
        sa.Column(
            "transient_publish_retry_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column("recovery_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("manifest_published_at", sa.DateTime(timezone=True)),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True)),
        sa.Column("progress_stage", sa.String(40)),
        sa.Column("progress_percent", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error_code", sa.String(60)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(CHANNEL_CHECK, name="ck_miniapp_publish_jobs_channel"),
        sa.CheckConstraint(
            "operation IN ('publish', 'rollback')", name="ck_miniapp_publish_jobs_operation"
        ),
        sa.CheckConstraint(
            "(operation = 'publish' AND expected_revision IS NOT NULL) OR "
            "(operation = 'rollback' AND expected_revision IS NULL "
            "AND target_version IS NOT NULL)",
            name="ck_miniapp_publish_jobs_operation_input",
        ),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'rolled_back')",
            name="ck_miniapp_publish_jobs_status",
        ),
        sa.CheckConstraint(
            "progress_percent BETWEEN 0 AND 100",
            name="ck_miniapp_publish_jobs_progress_percent",
        ),
        sa.CheckConstraint(
            "attempt_generation >= 1",
            name="ck_miniapp_publish_jobs_attempt_generation",
        ),
        sa.CheckConstraint(
            "transient_publish_retry_count >= 0",
            name="ck_miniapp_publish_jobs_transient_retry_count",
        ),
        sa.CheckConstraint(
            "recovery_count >= 0",
            name="ck_miniapp_publish_jobs_recovery_count",
        ),
        sa.UniqueConstraint(
            "idempotency_scope", "idempotency_key", name="uq_miniapp_publish_jobs_idempotency"
        ),
    )
    op.create_index(
        "uq_miniapp_publish_jobs_active_channel",
        "miniapp_publish_jobs",
        ["channel"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )

    op.create_table(
        "miniapp_publish_outbox",
        sa.Column("outbox_id", sa.String(24), primary_key=True),
        sa.Column(
            "job_id",
            sa.String(24),
            sa.ForeignKey("miniapp_publish_jobs.job_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(20), nullable=False, server_default="pending"),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("attempt_generation", sa.Integer(), nullable=False, server_default="1"),
        sa.Column(
            "attempt_token",
            sa.String(32),
            nullable=False,
            server_default="00000000000000000000000000000000",
        ),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("claimed_at", sa.DateTime(timezone=True)),
        sa.Column("delivered_at", sa.DateTime(timezone=True)),
        sa.Column("last_error", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.CheckConstraint(
            "status IN ('pending', 'claiming', 'delivered')",
            name="ck_miniapp_publish_outbox_status",
        ),
        sa.CheckConstraint(
            "attempt_count >= 0", name="ck_miniapp_publish_outbox_attempt_count"
        ),
        sa.CheckConstraint(
            "attempt_generation >= 1",
            name="ck_miniapp_publish_outbox_attempt_generation",
        ),
        sa.UniqueConstraint(
            "job_id", "attempt_generation", name="uq_miniapp_publish_outbox_job_generation"
        ),
    )
    op.create_index(
        "ix_miniapp_publish_outbox_claim",
        "miniapp_publish_outbox",
        ["status", "available_at", "outbox_id"],
    )


def downgrade() -> None:
    op.drop_table("miniapp_publish_outbox")
    op.drop_table("miniapp_publish_jobs")
    op.drop_table("owner_cases")
    op.drop_table("miniapp_media")
    op.drop_table("miniapp_channel_configs")
    op.drop_table("miniapp_content_workspaces")
    op.drop_table("miniapp_content_releases")
