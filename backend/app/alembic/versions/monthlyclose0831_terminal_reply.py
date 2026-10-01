"""persist canonical privacy-safe monthly-close assistant replies

Revision ID: monthlyclose0831reply
Revises: monthlyclose0831analysis
Create Date: 2026-08-31
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql


revision: str = "monthlyclose0831reply"
down_revision: Union[str, None] = "monthlyclose0831analysis"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "monthly_close_agent_runs",
        sa.Column(
            "output_payload_redacted",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("monthly_close_agent_runs", "output_payload_redacted")
