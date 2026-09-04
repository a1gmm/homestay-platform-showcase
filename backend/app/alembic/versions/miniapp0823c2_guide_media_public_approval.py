"""Persist guide media public approval identity.

Revision ID: miniapp0823c2
Revises: miniapp0823c1
Create Date: 2026-08-23
"""

from alembic import op
import sqlalchemy as sa


revision = "miniapp0823c2"
down_revision = "miniapp0823c1"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "miniapp_media",
        sa.Column("public_approved_by", sa.String(20), nullable=True),
    )
    op.add_column(
        "miniapp_media",
        sa.Column("public_approved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_miniapp_media_public_approved_by",
        "miniapp_media",
        "users",
        ["public_approved_by"],
        ["user_id"],
    )
    op.create_check_constraint(
        "ck_miniapp_media_public_approval_pair",
        "miniapp_media",
        "(public_approved_by IS NULL AND public_approved_at IS NULL) OR "
        "(public_approved_by IS NOT NULL AND public_approved_at IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_miniapp_media_public_approval_pair",
        "miniapp_media",
        type_="check",
    )
    op.drop_constraint(
        "fk_miniapp_media_public_approved_by",
        "miniapp_media",
        type_="foreignkey",
    )
    op.drop_column("miniapp_media", "public_approved_at")
    op.drop_column("miniapp_media", "public_approved_by")
