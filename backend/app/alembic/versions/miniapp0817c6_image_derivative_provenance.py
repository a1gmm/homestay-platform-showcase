"""Persist server-owned image derivative provenance."""

from alembic import op
import sqlalchemy as sa


revision = "miniapp0817c6"
down_revision = "miniapp0817c5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("miniapp_media", sa.Column("derivative_set_id", sa.String(24)))
    op.add_column("miniapp_media", sa.Column("derivative_role", sa.String(10)))
    op.add_column("miniapp_media", sa.Column("source_sha256", sa.String(64)))
    op.create_check_constraint(
        "ck_miniapp_media_derivative_provenance",
        "miniapp_media",
        "(derivative_set_id IS NULL AND derivative_role IS NULL AND source_sha256 IS NULL) OR "
        "(media_type = 'image' AND derivative_set_id IS NOT NULL "
        "AND derivative_role IS NOT NULL "
        "AND derivative_role IN ('thumbnail', 'display') "
        "AND source_sha256 IS NOT NULL AND length(source_sha256) = 64 "
        "AND size_bytes > 0)",
    )
    op.create_unique_constraint(
        "uq_miniapp_media_derivative_set_role",
        "miniapp_media",
        ["derivative_set_id", "derivative_role"],
    )
    op.create_index(
        "ix_miniapp_media_source_sha256", "miniapp_media", ["source_sha256"]
    )


def downgrade() -> None:
    op.drop_index("ix_miniapp_media_source_sha256", table_name="miniapp_media")
    op.drop_constraint(
        "uq_miniapp_media_derivative_set_role", "miniapp_media", type_="unique"
    )
    op.drop_constraint(
        "ck_miniapp_media_derivative_provenance", "miniapp_media", type_="check"
    )
    op.drop_column("miniapp_media", "source_sha256")
    op.drop_column("miniapp_media", "derivative_role")
    op.drop_column("miniapp_media", "derivative_set_id")
