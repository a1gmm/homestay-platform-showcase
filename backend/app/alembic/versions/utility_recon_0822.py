"""add utility reconciliation tables

Revision ID: utility_recon_0822
Revises: a817pg17
Create Date: 2026-08-22
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "utility_recon_0822"
down_revision = "a817pg17"
branch_labels = None
depends_on = None


def upgrade() -> None:
    json_type = postgresql.JSONB(astext_type=sa.Text())
    op.create_table(
        "utility_recon_uploads",
        sa.Column("upload_id", sa.String(40), primary_key=True),
        sa.Column("receipt_filename", sa.String(255), nullable=False),
        sa.Column("expense_filename", sa.String(255), nullable=False),
        sa.Column("file_fingerprints", json_type, nullable=False),
        sa.Column("role_mapping", json_type, nullable=False),
        sa.Column("receipt_months", json_type, nullable=False),
        sa.Column("expense_months", json_type, nullable=False),
        sa.Column("common_months", json_type, nullable=False),
        sa.Column("preflight_stats", json_type, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(20), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_utility_recon_uploads_created_at", "utility_recon_uploads", ["created_at"])
    op.create_table(
        "utility_recon_batches",
        sa.Column("batch_id", sa.String(40), primary_key=True),
        sa.Column("upload_id", sa.String(40), sa.ForeignKey("utility_recon_uploads.upload_id", ondelete="CASCADE"), nullable=False),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("raw_receipt_total", sa.Numeric(14, 2), nullable=False),
        sa.Column("raw_expense_total", sa.Numeric(14, 2), nullable=False),
        sa.Column("raw_difference", sa.Numeric(14, 2), nullable=False),
        sa.Column("corrected_difference", sa.Numeric(14, 2), nullable=False),
        sa.Column("raw_summary", json_type, nullable=False),
        sa.Column("corrected_summary", json_type, nullable=False),
        sa.Column("anomaly_counts", json_type, nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("closed_by", sa.String(20), sa.ForeignKey("users.user_id")),
        sa.Column("closed_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("upload_id", "month", name="uq_utility_recon_batch_upload_month"),
    )
    op.create_index("ix_utility_recon_batches_month_status", "utility_recon_batches", ["month", "status"])
    op.create_table(
        "utility_recon_rows",
        sa.Column("row_id", sa.String(40), primary_key=True),
        sa.Column("batch_id", sa.String(40), sa.ForeignKey("utility_recon_batches.batch_id", ondelete="CASCADE"), nullable=False),
        sa.Column("side", sa.String(10), nullable=False),
        sa.Column("business_date", sa.Date()),
        sa.Column("month", sa.String(7), nullable=False),
        sa.Column("floor", sa.String(30)),
        sa.Column("room", sa.String(50)),
        sa.Column("category", sa.String(20)),
        sa.Column("amount", sa.Numeric(14, 2)),
        sa.Column("source_filename", sa.String(255), nullable=False),
        sa.Column("source_sheet", sa.String(255), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("raw_values", json_type, nullable=False),
        sa.Column("customer_name", sa.String(100)),
        sa.Column("normalization_warnings", json_type, nullable=False),
        sa.Column("disposition", sa.String(20), nullable=False),
        sa.Column("exclusion_reason", sa.Text()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_utility_recon_rows_batch_side_date", "utility_recon_rows", ["batch_id", "side", "business_date"])
    op.create_table(
        "utility_recon_suggestions",
        sa.Column("suggestion_id", sa.String(40), primary_key=True),
        sa.Column("batch_id", sa.String(40), sa.ForeignKey("utility_recon_batches.batch_id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("related_row_ids", json_type, nullable=False),
        sa.Column("patch", json_type, nullable=False),
        sa.Column("evidence", json_type, nullable=False),
        sa.Column("confidence", sa.String(10), nullable=False),
        sa.Column("impact", json_type, nullable=False),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("decided_by", sa.String(20), sa.ForeignKey("users.user_id")),
        sa.Column("decided_at", sa.DateTime(timezone=True)),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_utility_recon_suggestions_batch_status", "utility_recon_suggestions", ["batch_id", "status"])


def downgrade() -> None:
    op.drop_index("ix_utility_recon_suggestions_batch_status", table_name="utility_recon_suggestions")
    op.drop_table("utility_recon_suggestions")
    op.drop_index("ix_utility_recon_rows_batch_side_date", table_name="utility_recon_rows")
    op.drop_table("utility_recon_rows")
    op.drop_index("ix_utility_recon_batches_month_status", table_name="utility_recon_batches")
    op.drop_table("utility_recon_batches")
    op.drop_index("ix_utility_recon_uploads_created_at", table_name="utility_recon_uploads")
    op.drop_table("utility_recon_uploads")
