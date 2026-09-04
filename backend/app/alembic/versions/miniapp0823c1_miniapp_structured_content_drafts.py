"""Persist structured stay-guide and travel drafts.

Revision ID: miniapp0823c1
Revises: billrecon0822
Create Date: 2026-08-23
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision = "miniapp0823c1"
down_revision = "billrecon0822"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "miniapp_structured_drafts",
        sa.Column("channel", sa.String(20), nullable=False),
        sa.Column("payload", postgresql.JSONB, nullable=False),
        sa.Column("updated_by", sa.String(20), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.ForeignKeyConstraint(
            ["channel"],
            ["miniapp_content_workspaces.channel"],
            name="fk_miniapp_structured_drafts_workspace",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["updated_by"],
            ["users.user_id"],
            name="fk_miniapp_structured_drafts_updated_by",
        ),
        sa.PrimaryKeyConstraint(
            "channel", name="pk_miniapp_structured_drafts"
        ),
        sa.CheckConstraint(
            "channel IN ('stay_guide', 'travel')",
            name="ck_miniapp_structured_drafts_channel",
        ),
    )


def downgrade() -> None:
    op.drop_table("miniapp_structured_drafts")
