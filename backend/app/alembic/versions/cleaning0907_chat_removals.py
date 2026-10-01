"""Recoverable removal of historical cleaning records from conversation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "cleaning0907chatremovals"
down_revision = "cleaning0907resolutions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "cleaning_work_removals",
        sa.Column("removal_id", sa.String(24), primary_key=True),
        sa.Column(
            "import_id",
            sa.String(24),
            sa.ForeignKey("cleaning_work_imports.import_id"),
            nullable=False,
        ),
        sa.Column(
            "document_id",
            sa.String(24),
            sa.ForeignKey("monthly_close_documents.document_id"),
            nullable=False,
        ),
        sa.Column("record_type", sa.String(24), nullable=False),
        sa.Column("record_id", sa.String(24), nullable=False),
        sa.Column("snapshot", postgresql.JSONB(), nullable=False),
        sa.Column(
            "deleted_by", sa.String(20), sa.ForeignKey("users.user_id"), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.Column("restored_at", sa.DateTime(timezone=True)),
        sa.Column("restored_by", sa.String(20), sa.ForeignKey("users.user_id")),
    )
    op.create_index(
        "ix_cleaning_work_removals_document_id",
        "cleaning_work_removals",
        ["document_id"],
    )


def downgrade():
    op.drop_table("cleaning_work_removals")
