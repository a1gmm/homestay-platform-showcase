"""Persist date-level historical cleaning evidence and idempotent imports.

Revision ID: cleaning0907workrecords
Revises: merge0903monthlyroomswap
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "cleaning0907workrecords"
down_revision = "merge0903monthlyroomswap"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cleaning_work_imports",
        sa.Column("import_id", sa.String(24), primary_key=True),
        sa.Column(
            "cycle_id",
            sa.String(24),
            sa.ForeignKey("monthly_close_cycles.cycle_id"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.String(24),
            sa.ForeignKey("monthly_close_documents.document_id"),
            nullable=False,
        ),
        sa.Column("request_id", sa.String(80), nullable=False),
        sa.Column("preview_hash", sa.String(64), nullable=False),
        sa.Column(
            "created_by", sa.String(20), sa.ForeignKey("users.user_id"), nullable=False
        ),
        sa.Column("result", postgresql.JSONB(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "cycle_id", "request_id", name="uq_cleaning_work_import_request"
        ),
    )
    op.create_table(
        "cleaning_work_records",
        sa.Column("record_id", sa.String(24), primary_key=True),
        sa.Column(
            "import_id",
            sa.String(24),
            sa.ForeignKey("cleaning_work_imports.import_id"),
            nullable=False,
        ),
        sa.Column(
            "room_id", sa.String(10), sa.ForeignKey("rooms.room_id"), nullable=False
        ),
        sa.Column("service_date", sa.Date(), nullable=False),
        sa.Column("service_type", sa.String(24), nullable=False),
        sa.Column(
            "document_id",
            sa.String(24),
            sa.ForeignKey("monthly_close_documents.document_id"),
            nullable=False,
        ),
        sa.Column("document_sha256", sa.String(64), nullable=False),
        sa.Column("source_sheet", sa.String(255), nullable=False),
        sa.Column("source_row", sa.Integer(), nullable=False),
        sa.Column(
            "created_by", sa.String(20), sa.ForeignKey("users.user_id"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.UniqueConstraint(
            "room_id",
            "service_date",
            "service_type",
            name="uq_cleaning_work_record_event",
        ),
        sa.CheckConstraint(
            "service_type IN ('cleaning', 'instay_cleaning')",
            name="ck_cleaning_work_record_type",
        ),
    )
    op.create_index(
        "ix_cleaning_work_records_service_date",
        "cleaning_work_records",
        ["service_date"],
    )

    op.create_index(
        "ix_cleaning_work_records_document_id", "cleaning_work_records", ["document_id"]
    )
    op.create_index(
        "ix_cleaning_work_imports_document_id", "cleaning_work_imports", ["document_id"]
    )


def downgrade():
    op.drop_table("cleaning_work_records")
    op.drop_table("cleaning_work_imports")
