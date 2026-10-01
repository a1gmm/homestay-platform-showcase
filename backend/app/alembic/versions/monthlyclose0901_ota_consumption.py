"""add immutable one-row-to-one-appeal OTA settlement consumption

Revision ID: monthlyclose0901otaconsume
Revises: monthlyclose0901binding
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "monthlyclose0901otaconsume"
down_revision: Union[str, None] = "monthlyclose0901binding"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "monthly_close_ota_settlement_consumptions",
        sa.Column("consumption_id", sa.String(length=24), nullable=False),
        sa.Column("row_identity_hash", sa.String(length=64), nullable=False),
        sa.Column("row_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("settlement_evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("later_cycle_id", sa.String(length=24), nullable=False),
        sa.Column("later_document_id", sa.String(length=24), nullable=False),
        sa.Column("later_document_sha256", sa.String(length=64), nullable=False),
        sa.Column("later_batch_id", sa.String(length=40), nullable=False),
        sa.Column("later_batch_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("platform_namespace", sa.String(length=80), nullable=False),
        sa.Column("source_row_index", sa.Integer(), nullable=False),
        sa.Column("source_row_hash", sa.String(length=64), nullable=False),
        sa.Column("economic_facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("original_cycle_id", sa.String(length=24), nullable=False),
        sa.Column("original_issue_id", sa.String(length=24), nullable=False),
        sa.Column("original_batch_id", sa.String(length=40), nullable=False),
        sa.Column("original_diff_id", sa.String(length=40), nullable=False),
        sa.Column("proposal_id", sa.String(length=24), nullable=False),
        sa.Column("attempt_id", sa.String(length=24), nullable=False),
        sa.Column("verification_id", sa.String(length=24), nullable=False),
        sa.Column("request_id", sa.String(length=80), nullable=False),
        sa.Column("created_by", sa.String(length=20), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["later_cycle_id"], ["monthly_close_cycles.cycle_id"]),
        sa.ForeignKeyConstraint(["later_document_id"], ["monthly_close_documents.document_id"]),
        sa.ForeignKeyConstraint(["later_batch_id"], ["recon_batches.batch_id"]),
        sa.ForeignKeyConstraint(["original_cycle_id"], ["monthly_close_cycles.cycle_id"]),
        sa.ForeignKeyConstraint(["original_issue_id"], ["monthly_close_issue_instances.issue_id"]),
        sa.ForeignKeyConstraint(["original_batch_id"], ["recon_batches.batch_id"]),
        sa.ForeignKeyConstraint(["original_diff_id"], ["recon_diffs.diff_id"]),
        sa.ForeignKeyConstraint(["proposal_id"], ["monthly_close_action_proposals.proposal_id"]),
        sa.ForeignKeyConstraint(["attempt_id"], ["monthly_close_execution_attempts.attempt_id"]),
        sa.ForeignKeyConstraint(["verification_id"], ["monthly_close_verifications.verification_id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("consumption_id"),
        sa.UniqueConstraint("row_identity_hash", name="uq_mc_ota_consumption_row_identity"),
        sa.UniqueConstraint(
            "later_cycle_id",
            "later_document_id",
            "later_batch_id",
            "platform_namespace",
            "source_row_index",
            name="uq_mc_ota_consumption_source_row",
        ),
        sa.UniqueConstraint("original_issue_id", name="uq_mc_ota_consumption_issue"),
    )
    op.create_index(
        "ix_mc_ota_consumption_later_cycle",
        "monthly_close_ota_settlement_consumptions",
        ["later_cycle_id"],
    )
    op.create_index(
        "ix_mc_ota_consumption_original_cycle",
        "monthly_close_ota_settlement_consumptions",
        ["original_cycle_id"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_mc_ota_consumption_original_cycle",
        table_name="monthly_close_ota_settlement_consumptions",
    )
    op.drop_index(
        "ix_mc_ota_consumption_later_cycle",
        table_name="monthly_close_ota_settlement_consumptions",
    )
    op.drop_table("monthly_close_ota_settlement_consumptions")
