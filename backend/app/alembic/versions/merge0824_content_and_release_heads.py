"""Merge miniapp content and release announcement migration heads.

Revision ID: merge0824contentrelease
Revises: miniapp0823c4, releaseannounce0822
Create Date: 2026-08-24
"""


revision = "merge0824contentrelease"
down_revision = ("miniapp0823c4", "releaseannounce0822")
branch_labels = None
depends_on = None


def upgrade() -> None:
    """Join the two migration branches without changing schema."""


def downgrade() -> None:
    """Split the migration graph back into its two parent heads."""
