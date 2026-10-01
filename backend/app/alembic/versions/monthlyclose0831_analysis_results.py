"""add durable monthly-close analysis results and generations

Revision ID: monthlyclose0831analysis
Revises: monthlyclose0831control
Create Date: 2026-08-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "monthlyclose0831analysis"
down_revision: Union[str, None] = "monthlyclose0831control"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column(
        "monthly_close_inbox_items",
        sa.Column(
            "classification_generation",
            sa.Integer(),
            nullable=False,
            server_default="1",
        ),
    )
    op.create_check_constraint(
        "ck_monthly_close_inbox_classification_generation",
        "monthly_close_inbox_items",
        "classification_generation >= 1",
    )
    op.create_unique_constraint(
        "uq_monthly_close_document_id_cycle",
        "monthly_close_documents",
        ["document_id", "cycle_id"],
    )
    op.create_unique_constraint(
        "uq_monthly_close_job_id_cycle",
        "monthly_close_processing_jobs",
        ["job_id", "cycle_id"],
    )
    op.create_table(
        "monthly_close_document_analyses",
        sa.Column("analysis_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("document_id", sa.String(length=24), nullable=False),
        sa.Column("job_id", sa.String(length=24), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("classification_generation", sa.Integer(), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("analyzer_version", sa.String(length=80), nullable=False),
        sa.Column("result_schema_version", sa.String(length=80), nullable=False),
        sa.Column("mapping_version", sa.String(length=80), nullable=False),
        sa.Column("dedupe_key", sa.String(length=160), nullable=False),
        sa.Column(
            "status",
            sa.String(length=20),
            nullable=False,
            server_default="queued",
        ),
        sa.Column("result", JSONB, nullable=True),
        sa.Column("error_code", sa.String(length=80), nullable=True),
        sa.Column("manual_action", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "generation >= 1",
            name="ck_monthly_close_analysis_generation",
        ),
        sa.CheckConstraint(
            "classification_generation >= 1",
            name="ck_monthly_close_analysis_classification_generation",
        ),
        sa.CheckConstraint(
            "status IN "
            "('queued', 'processing', 'needs_review', 'completed', 'failed_safe')",
            name="ck_monthly_close_analysis_status",
        ),
        sa.CheckConstraint(
            "source_type IN "
            "('cleaning_statement', 'linen_statement', 'utility_receipt', "
            "'utility_expense', 'ota_statement', 'operating_expenses')",
            name="ck_monthly_close_analysis_source_type",
        ),
        sa.ForeignKeyConstraint(
            ["cycle_id"],
            ["monthly_close_cycles.cycle_id"],
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["document_id", "cycle_id"],
            [
                "monthly_close_documents.document_id",
                "monthly_close_documents.cycle_id",
            ],
            name="fk_monthly_close_analysis_document_cycle",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["job_id", "cycle_id"],
            [
                "monthly_close_processing_jobs.job_id",
                "monthly_close_processing_jobs.cycle_id",
            ],
            name="fk_monthly_close_analysis_job_cycle",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("analysis_id"),
        sa.UniqueConstraint(
            "job_id", name="uq_monthly_close_document_analyses_job_id"
        ),
        sa.UniqueConstraint(
            "document_id",
            "generation",
            name="uq_monthly_close_analysis_document_generation",
        ),
        sa.UniqueConstraint(
            "cycle_id",
            "dedupe_key",
            name="uq_monthly_close_analysis_cycle_dedupe",
        ),
    )
    op.create_index(
        "ix_monthly_close_analysis_document_generation",
        "monthly_close_document_analyses",
        ["document_id", "generation"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_monthly_close_analysis_document_generation",
        table_name="monthly_close_document_analyses",
    )
    op.drop_table("monthly_close_document_analyses")
    op.drop_constraint(
        "uq_monthly_close_job_id_cycle",
        "monthly_close_processing_jobs",
        type_="unique",
    )
    op.drop_constraint(
        "uq_monthly_close_document_id_cycle",
        "monthly_close_documents",
        type_="unique",
    )
    op.drop_constraint(
        "ck_monthly_close_inbox_classification_generation",
        "monthly_close_inbox_items",
        type_="check",
    )
    op.drop_column("monthly_close_inbox_items", "classification_generation")
