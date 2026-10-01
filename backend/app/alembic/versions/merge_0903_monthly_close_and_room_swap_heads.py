"""merge monthly-close and room-swap migration heads

Revision ID: merge0903monthlyroomswap
Revises: monthlyclose0901otaconsume, roomswap0902defer
Create Date: 2026-09-03
"""

revision = "merge0903monthlyroomswap"
down_revision = ("monthlyclose0901otaconsume", "roomswap0902defer")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the monthly-close and room-swap branches without changing schema."""


def downgrade() -> None:
    """Split the migration graph back into its two parent heads."""
