"""administrator-only nine-step monthly close

Revision ID: monthlyclose0826
Revises: merge0824contentrelease
Create Date: 2026-08-26
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "monthlyclose0826"
down_revision: Union[str, None] = "merge0824contentrelease"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_STEP_KEYS = (
    "source_collection",
    "order_integrity",
    "service_fees",
    "utilities",
    "ota_statements",
    "exception_clearance",
    "preflight",
    "settlement_review",
    "owner_confirmation",
)
_SOURCE_TYPES = (
    "cleaning_statement",
    "linen_statement",
    "utility_receipt",
    "utility_expense",
    "ota_statement",
    "operating_expenses",
)


def _sql_values(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.create_table(
        "monthly_close_cycles",
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("billing_month", sa.String(length=7), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="open", nullable=False),
        sa.Column("final_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("completed_by", sa.String(length=20), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopened_by", sa.String(length=20), nullable=True),
        sa.Column("reopened_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reopen_reason", sa.Text(), nullable=True),
        sa.Column("created_by", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('open', 'completed', 'reopened')", name="ck_monthly_close_cycles_status"),
        sa.ForeignKeyConstraint(["completed_by"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["reopened_by"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("cycle_id"),
        sa.UniqueConstraint("billing_month", name="uq_monthly_close_cycles_month"),
    )
    op.create_table(
        "monthly_close_source_requirements",
        sa.Column("requirement_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("state", sa.String(length=20), server_default="pending", nullable=False),
        sa.Column("not_applicable_reason", sa.Text(), nullable=True),
        sa.Column("decided_by", sa.String(length=20), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"source_type IN ({_sql_values(_SOURCE_TYPES)})", name="ck_monthly_close_source_type"),
        sa.CheckConstraint("state IN ('pending', 'uploaded', 'not_applicable')", name="ck_monthly_close_source_state"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("requirement_id"),
        sa.UniqueConstraint("cycle_id", "source_type", name="uq_monthly_close_source_cycle_type"),
    )
    op.create_table(
        "monthly_close_documents",
        sa.Column("document_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("processing_status", sa.String(length=20), server_default="stored", nullable=False),
        sa.Column("processing_error", sa.Text(), nullable=True),
        sa.Column("engine_type", sa.String(length=40), nullable=True),
        sa.Column("engine_id", sa.String(length=40), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("is_active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("invalidated_by", sa.String(length=20), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("uploaded_by", sa.String(length=20), nullable=True),
        sa.Column("uploaded_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"source_type IN ({_sql_values(_SOURCE_TYPES)})", name="ck_monthly_close_document_source_type"),
        sa.CheckConstraint("processing_status IN ('stored', 'processed', 'rejected')", name="ck_monthly_close_document_status"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["invalidated_by"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["uploaded_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("document_id"),
        sa.UniqueConstraint("cycle_id", "sha256", name="uq_monthly_close_document_cycle_sha"),
    )
    op.create_index("ix_monthly_close_document_cycle_source", "monthly_close_documents", ["cycle_id", "source_type", "is_active"])
    op.create_table(
        "monthly_close_step_confirmations",
        sa.Column("confirmation_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("step_key", sa.String(length=40), nullable=False),
        sa.Column("evidence_hash", sa.String(length=64), nullable=False),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("history", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'[]'::jsonb"), nullable=False),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column("confirmed_by", sa.String(length=20), nullable=True),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint(f"step_key IN ({_sql_values(_STEP_KEYS)})", name="ck_monthly_close_step_key"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["confirmed_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("confirmation_id"),
        sa.UniqueConstraint("cycle_id", "step_key", name="uq_monthly_close_step_cycle_key"),
    )
    op.create_index("ix_monthly_close_step_cycle", "monthly_close_step_confirmations", ["cycle_id"])
    op.create_table(
        "monthly_close_service_lines",
        sa.Column("line_id", sa.String(length=24), nullable=False),
        sa.Column("document_id", sa.String(length=24), nullable=False),
        sa.Column("service_date", sa.Date(), nullable=True),
        sa.Column("room_ref", sa.String(length=100), nullable=True),
        sa.Column("order_ref", sa.String(length=100), nullable=True),
        sa.Column("service_type", sa.String(length=40), nullable=False),
        sa.Column("quantity", sa.Numeric(precision=12, scale=3), nullable=True),
        sa.Column("unit_price", sa.Numeric(precision=12, scale=2), nullable=True),
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=False),
        sa.Column("source_sheet", sa.String(length=255), nullable=False),
        sa.Column("source_row_number", sa.Integer(), nullable=False),
        sa.Column("raw_values", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("business_key", sa.String(length=255), nullable=True),
        sa.Column("match_status", sa.String(length=24), server_default="pending", nullable=False),
        sa.Column("issue_code", sa.String(length=60), nullable=True),
        sa.Column("matched_expense_id", sa.String(length=20), nullable=True),
        sa.Column("match_detail", postgresql.JSONB(astext_type=sa.Text()), server_default=sa.text("'{}'::jsonb"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("match_status IN ('pending', 'matched', 'amount_mismatch', 'vendor_only', 'system_only', 'ambiguous', 'duplicate')", name="ck_monthly_close_service_line_match_status"),
        sa.ForeignKeyConstraint(["document_id"], ["monthly_close_documents.document_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["matched_expense_id"], ["expenses.expense_id"]),
        sa.PrimaryKeyConstraint("line_id"),
        sa.UniqueConstraint("document_id", "source_sheet", "source_row_number", name="uq_monthly_close_service_line_source_row"),
    )
    op.create_index("ix_monthly_close_service_line_document_status", "monthly_close_service_lines", ["document_id", "match_status"])


def downgrade() -> None:
    op.drop_index("ix_monthly_close_service_line_document_status", table_name="monthly_close_service_lines")
    op.drop_table("monthly_close_service_lines")
    op.drop_index("ix_monthly_close_step_cycle", table_name="monthly_close_step_confirmations")
    op.drop_table("monthly_close_step_confirmations")
    op.drop_index("ix_monthly_close_document_cycle_source", table_name="monthly_close_documents")
    op.drop_table("monthly_close_documents")
    op.drop_table("monthly_close_source_requirements")
    op.drop_table("monthly_close_cycles")
