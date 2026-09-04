"""scope cleaning expense uniqueness to August 2026 onward

Revision ID: cleaning0830cutoff
Revises: cleaning0829expense
Create Date: 2026-08-30
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "cleaning0830cutoff"
down_revision: Union[str, None] = "cleaning0829expense"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.drop_constraint(
        "uq_cleaning_requests_expense_id",
        "cleaning_requests",
        type_="unique",
    )
    op.create_index(
        "uq_cleaning_requests_expense_id_from_202608",
        "cleaning_requests",
        ["expense_id"],
        unique=True,
        postgresql_where=sa.text("request_date >= DATE '2026-08-01'"),
        sqlite_where=sa.text("request_date >= '2026-08-01'"),
    )


def downgrade() -> None:
    op.drop_index(
        "uq_cleaning_requests_expense_id_from_202608",
        table_name="cleaning_requests",
    )
    op.create_unique_constraint(
        "uq_cleaning_requests_expense_id",
        "cleaning_requests",
        ["expense_id"],
    )
