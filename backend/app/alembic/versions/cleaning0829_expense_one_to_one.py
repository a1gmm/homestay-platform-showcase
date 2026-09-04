"""enforce one cleaning request per expense

Revision ID: cleaning0829expense
Revises: monthlyclose0827
Create Date: 2026-08-29
"""

from typing import Sequence, Union

from alembic import op


revision: str = "cleaning0829expense"
down_revision: Union[str, None] = "monthlyclose0827"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_cleaning_requests_expense_id",
        "cleaning_requests",
        ["expense_id"],
    )


def downgrade() -> None:
    op.drop_constraint(
        "uq_cleaning_requests_expense_id",
        "cleaning_requests",
        type_="unique",
    )
