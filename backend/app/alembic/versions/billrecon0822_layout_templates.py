"""Persist confirmed reusable billing workbook layouts.

Revision ID: billrecon0822
Revises: d7dc2d305fcd
Create Date: 2026-08-22
"""
import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision = "billrecon0822"
down_revision = "d7dc2d305fcd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("SET lock_timeout = '5s'")
    op.create_table(
        "recon_layout_templates",
        sa.Column("template_id", sa.String(40), primary_key=True),
        sa.Column("layout_signature", sa.String(64), nullable=False, unique=True),
        sa.Column("mapping", postgresql.JSONB, nullable=False, server_default="{}"),
        sa.Column("platform_scope", sa.String(20), nullable=False),
        sa.Column("created_by", sa.String(20), sa.ForeignKey("users.user_id"), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("use_count", sa.Integer, nullable=False, server_default="0"),
        sa.Column("last_used_at", sa.DateTime(timezone=True)),
    )


def downgrade() -> None:
    op.drop_table("recon_layout_templates")
