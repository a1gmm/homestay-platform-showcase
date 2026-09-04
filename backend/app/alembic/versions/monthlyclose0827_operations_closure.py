"""monthly close durable intake and notification deep links

Revision ID: monthlyclose0827
Revises: monthlyclose0826
Create Date: 2026-08-27
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "monthlyclose0827"
down_revision: Union[str, None] = "monthlyclose0826"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


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
    op.add_column(
        "notifications", sa.Column("action_url", sa.String(length=500), nullable=True)
    )
    op.add_column(
        "monthly_close_cycles",
        sa.Column("monitor_state_hash", sa.String(length=64), nullable=True),
    )
    op.add_column(
        "monthly_close_cycles",
        sa.Column("monitor_state_since", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "monthly_close_cycles",
        sa.Column("monitor_effective_status", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "monthly_close_cycles",
        sa.Column("monitor_current_step", sa.String(length=40), nullable=True),
    )
    op.add_column(
        "monthly_close_cycles",
        sa.Column("monitor_progress", sa.Integer(), nullable=True),
    )
    op.add_column(
        "monthly_close_cycles",
        sa.Column("monitor_blocking_count", sa.Integer(), nullable=True),
    )
    op.create_table(
        "monthly_close_inbox_items",
        sa.Column("item_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("filename", sa.String(length=255), nullable=False),
        sa.Column("mime_type", sa.String(length=120), nullable=False),
        sa.Column("byte_size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("content", sa.LargeBinary(), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=True),
        sa.Column("confidence", sa.Numeric(precision=5, scale=4), nullable=True),
        sa.Column("suggested_by", sa.String(length=24), nullable=True),
        sa.Column("classification_reason", sa.Text(), nullable=True),
        sa.Column("status", sa.String(length=20), server_default="received", nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("origin", sa.String(length=20), server_default="admin", nullable=False),
        sa.Column("submitted_label", sa.String(length=120), nullable=True),
        sa.Column("created_by", sa.String(length=20), nullable=True),
        sa.Column("updated_by", sa.String(length=20), nullable=True),
        sa.Column("document_id", sa.String(length=24), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.CheckConstraint("status IN ('received', 'classified', 'needs_review', 'confirmed', 'failed', 'dismissed')", name="ck_monthly_close_inbox_status"),
        sa.CheckConstraint("origin IN ('admin', 'external')", name="ck_monthly_close_inbox_origin"),
        sa.CheckConstraint(f"source_type IS NULL OR source_type IN ({_sql_values(_SOURCE_TYPES)})", name="ck_monthly_close_inbox_source_type"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["updated_by"], ["users.user_id"]),
        sa.ForeignKeyConstraint(["document_id"], ["monthly_close_documents.document_id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("item_id"),
        sa.UniqueConstraint("cycle_id", "sha256", name="uq_monthly_close_inbox_cycle_sha"),
        sa.UniqueConstraint("document_id"),
    )
    op.create_index("ix_monthly_close_inbox_cycle_status", "monthly_close_inbox_items", ["cycle_id", "status"])
    op.create_table(
        "monthly_close_intake_links",
        sa.Column("link_id", sa.String(length=24), nullable=False),
        sa.Column("cycle_id", sa.String(length=24), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("source_type", sa.String(length=40), nullable=True),
        sa.Column("label", sa.String(length=120), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_by", sa.String(length=20), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("last_uploaded_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(f"source_type IS NULL OR source_type IN ({_sql_values(_SOURCE_TYPES)})", name="ck_monthly_close_intake_source_type"),
        sa.ForeignKeyConstraint(["cycle_id"], ["monthly_close_cycles.cycle_id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["created_by"], ["users.user_id"]),
        sa.PrimaryKeyConstraint("link_id"),
        sa.UniqueConstraint("token_hash", name="uq_monthly_close_intake_token_hash"),
    )
    op.create_index("ix_monthly_close_intake_cycle", "monthly_close_intake_links", ["cycle_id", "revoked_at"])


def downgrade() -> None:
    op.drop_index("ix_monthly_close_intake_cycle", table_name="monthly_close_intake_links")
    op.drop_table("monthly_close_intake_links")
    op.drop_index("ix_monthly_close_inbox_cycle_status", table_name="monthly_close_inbox_items")
    op.drop_table("monthly_close_inbox_items")
    op.drop_column("monthly_close_cycles", "monitor_blocking_count")
    op.drop_column("monthly_close_cycles", "monitor_progress")
    op.drop_column("monthly_close_cycles", "monitor_current_step")
    op.drop_column("monthly_close_cycles", "monitor_effective_status")
    op.drop_column("monthly_close_cycles", "monitor_state_since")
    op.drop_column("monthly_close_cycles", "monitor_state_hash")
    op.drop_column("notifications", "action_url")
