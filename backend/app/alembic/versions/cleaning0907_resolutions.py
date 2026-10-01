"""Record administrator reconciliation decisions and date-level visit quantities."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "cleaning0907resolutions"
down_revision = "cleaning0907workrecords"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "cleaning_work_records",
        sa.Column("quantity", sa.Integer(), nullable=False, server_default="1"),
    )
    op.create_check_constraint(
        "ck_cleaning_work_record_quantity",
        "cleaning_work_records",
        "quantity >= 1 AND quantity <= 5000",
    )
    op.create_table(
        "cleaning_work_resolutions",
        sa.Column("resolution_id", sa.String(24), primary_key=True),
        sa.Column(
            "import_id",
            sa.String(24),
            sa.ForeignKey("cleaning_work_imports.import_id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "document_id",
            sa.String(24),
            sa.ForeignKey("monthly_close_documents.document_id"),
            nullable=False,
        ),
        sa.Column("service_date", sa.Date(), nullable=False),
        sa.Column(
            "room_id", sa.String(10), sa.ForeignKey("rooms.room_id"), nullable=False
        ),
        sa.Column("service_type", sa.String(24), nullable=False),
        sa.Column("evidence_hash", sa.String(64), nullable=False),
        sa.Column("decision", sa.String(24), nullable=False),
        sa.Column("confirmed_count", sa.Integer(), nullable=False),
        sa.Column("reason", sa.String(1000), nullable=False),
        sa.Column("evidence", postgresql.JSONB(), nullable=False),
        sa.Column(
            "confirmed_by",
            sa.String(20),
            sa.ForeignKey("users.user_id"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "confirmed_count >= 0 AND confirmed_count <= 5000",
            name="ck_cleaning_resolution_count",
        ),
        sa.CheckConstraint(
            "decision IN ('exclude_system', 'count_once', 'accept_table')",
            name="ck_cleaning_resolution_decision",
        ),
    )
    op.create_index(
        "ix_cleaning_work_resolutions_document_id",
        "cleaning_work_resolutions",
        ["document_id"],
    )


def downgrade():
    op.drop_table("cleaning_work_resolutions")
    op.drop_constraint(
        "ck_cleaning_work_record_quantity", "cleaning_work_records", type_="check"
    )
    op.drop_column("cleaning_work_records", "quantity")
