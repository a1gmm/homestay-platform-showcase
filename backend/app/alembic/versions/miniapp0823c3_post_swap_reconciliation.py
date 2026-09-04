"""Persist state required for exact post-swap compensation.

Revision ID: miniapp0823c3
Revises: miniapp0823c2
Create Date: 2026-08-23
"""

from alembic import op
import sqlalchemy as sa


revision = "miniapp0823c3"
down_revision = "miniapp0823c2"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "miniapp_publish_jobs",
        sa.Column("result_release_previous_status", sa.String(20), nullable=True),
    )
    op.add_column(
        "miniapp_publish_jobs",
        sa.Column(
            "auto_enabled_channel",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.create_check_constraint(
        "ck_miniapp_publish_jobs_result_previous_status",
        "miniapp_publish_jobs",
        "result_release_previous_status IS NULL OR "
        "result_release_previous_status IN ('published', 'superseded', 'rolled_back')",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_miniapp_publish_jobs_result_previous_status",
        "miniapp_publish_jobs",
        type_="check",
    )
    op.drop_column("miniapp_publish_jobs", "auto_enabled_channel")
    op.drop_column("miniapp_publish_jobs", "result_release_previous_status")
