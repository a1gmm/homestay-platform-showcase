"""Create durable account-level release announcement receipts.

Revision ID: releaseannounce0822
Revises: billrecon0822
Create Date: 2026-08-22
"""
import sqlalchemy as sa
from alembic import op


revision = "releaseannounce0822"
down_revision = "billrecon0822"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "release_announcement_receipts",
        sa.Column(
            "user_id",
            sa.String(20),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("announcement_id", sa.String(100), primary_key=True),
        sa.Column(
            "acknowledged_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )


def downgrade() -> None:
    op.drop_table("release_announcement_receipts")
