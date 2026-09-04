"""Fence legacy publish recovery and own auto-created channel configs.

Revision ID: miniapp0823c4
Revises: miniapp0823c3
Create Date: 2026-08-23
"""

from alembic import op
import sqlalchemy as sa


revision = "miniapp0823c4"
down_revision = "miniapp0823c3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "miniapp_publish_jobs",
        sa.Column("recovery_metadata_version", sa.Integer(), nullable=True),
    )
    op.add_column(
        "miniapp_publish_jobs",
        sa.Column("auto_enabled_config_token", sa.String(32), nullable=True),
    )
    op.add_column(
        "miniapp_publish_jobs",
        sa.Column(
            "auto_enabled_config_updated_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "miniapp_channel_configs",
        sa.Column("auto_creation_token", sa.String(32), nullable=True),
    )

    # Every pre-migration active job lacks the history needed to distinguish a
    # pre-swap failure from an acknowledged pointer write.  Terminate it for
    # manual inspection; never synthesize release or pointer history.
    op.execute(
        """
        UPDATE miniapp_publish_jobs
        SET status = 'failed',
            progress_stage = 'manual_recovery_required',
            progress_percent = 100,
            error_code = 'legacy_recovery_metadata_missing',
            heartbeat_at = now()
        WHERE status IN ('queued', 'running')
          AND recovery_metadata_version IS NULL
        """
    )

    op.create_check_constraint(
        "ck_miniapp_publish_jobs_recovery_metadata_version",
        "miniapp_publish_jobs",
        "recovery_metadata_version IS NULL OR recovery_metadata_version = 1",
    )
    op.create_check_constraint(
        "ck_miniapp_publish_jobs_active_recovery_metadata",
        "miniapp_publish_jobs",
        "status NOT IN ('queued', 'running') OR "
        "recovery_metadata_version IS NOT NULL",
    )
    op.create_check_constraint(
        "ck_miniapp_publish_jobs_auto_config_evidence",
        "miniapp_publish_jobs",
        "recovery_metadata_version IS NULL OR "
        "((auto_enabled_channel = false "
        "AND auto_enabled_config_token IS NULL "
        "AND auto_enabled_config_updated_at IS NULL) OR "
        "(auto_enabled_channel = true "
        "AND auto_enabled_config_token IS NOT NULL "
        "AND auto_enabled_config_updated_at IS NOT NULL))",
    )
    op.create_unique_constraint(
        "uq_miniapp_channel_configs_auto_creation_token",
        "miniapp_channel_configs",
        ["auto_creation_token"],
    )


def downgrade() -> None:
    # C3 cannot interpret the durable C4 recovery/auto-enable ownership facts.
    # Refuse before dropping any evidence so a C3 worker can never inherit an
    # active C4 job with an ambiguous post-swap state.
    op.execute(
        """
        DO $miniapp0823c4_downgrade$
        BEGIN
            IF EXISTS (
                SELECT 1
                FROM miniapp_publish_jobs
                WHERE status IN ('queued', 'running')
            ) THEN
                RAISE EXCEPTION
                    'miniapp0823c4 downgrade refused: active miniapp content jobs exist; '
                    'stop publish intake, drain or safely terminalize them with C4 code, '
                    'stop C4 API/Beat/content workers, then retry before starting C3 workers'
                    USING ERRCODE = '55000';
            END IF;
        END
        $miniapp0823c4_downgrade$;
        """
    )
    op.drop_constraint(
        "uq_miniapp_channel_configs_auto_creation_token",
        "miniapp_channel_configs",
        type_="unique",
    )
    op.drop_constraint(
        "ck_miniapp_publish_jobs_auto_config_evidence",
        "miniapp_publish_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_miniapp_publish_jobs_active_recovery_metadata",
        "miniapp_publish_jobs",
        type_="check",
    )
    op.drop_constraint(
        "ck_miniapp_publish_jobs_recovery_metadata_version",
        "miniapp_publish_jobs",
        type_="check",
    )
    op.drop_column("miniapp_channel_configs", "auto_creation_token")
    op.drop_column("miniapp_publish_jobs", "auto_enabled_config_updated_at")
    op.drop_column("miniapp_publish_jobs", "auto_enabled_config_token")
    op.drop_column("miniapp_publish_jobs", "recovery_metadata_version")
