"""bind every monthly-close authorization row to one immutable proposal digest

Revision ID: monthlyclose0901binding
Revises: monthlyclose0901attempts
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "monthlyclose0901binding"
down_revision: Union[str, None] = "monthlyclose0901attempts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


TABLES = (
    "monthly_close_action_proposals",
    "monthly_close_action_approvals",
    "monthly_close_approval_evaluations",
    "monthly_close_execution_attempts",
)
UNTRUSTED_BINDING_SENTINEL = "0" * 64


def upgrade() -> None:
    for table in TABLES:
        op.add_column(
            table,
            sa.Column("proposal_binding_hash", sa.String(length=64), nullable=True),
        )
        op.execute(
            sa.text(
                f"UPDATE {table} SET proposal_binding_hash = :sentinel "
                "WHERE proposal_binding_hash IS NULL"
            ).bindparams(sentinel=UNTRUSTED_BINDING_SENTINEL)
        )
        op.alter_column(
            table,
            "proposal_binding_hash",
            existing_type=sa.String(length=64),
            nullable=False,
        )

    op.create_unique_constraint(
        "uq_monthly_close_proposal_id_binding",
        "monthly_close_action_proposals",
        ["proposal_id", "proposal_binding_hash"],
    )
    op.create_foreign_key(
        "fk_monthly_close_approval_proposal_binding",
        "monthly_close_action_approvals",
        "monthly_close_action_proposals",
        ["proposal_id", "proposal_binding_hash"],
        ["proposal_id", "proposal_binding_hash"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        "fk_monthly_close_evaluation_proposal_binding",
        "monthly_close_approval_evaluations",
        "monthly_close_action_proposals",
        ["proposal_id", "proposal_binding_hash"],
        ["proposal_id", "proposal_binding_hash"],
        ondelete="CASCADE",
    )
    op.create_unique_constraint(
        "uq_monthly_close_evaluation_id_proposal_approval_binding",
        "monthly_close_approval_evaluations",
        [
            "evaluation_id",
            "proposal_id",
            "approval_set_hash",
            "proposal_binding_hash",
        ],
    )
    op.drop_constraint(
        "fk_monthly_close_attempt_evaluation_proposal",
        "monthly_close_execution_attempts",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_monthly_close_attempt_evaluation_proposal",
        "monthly_close_execution_attempts",
        "monthly_close_approval_evaluations",
        [
            "approval_evaluation_id",
            "proposal_id",
            "approval_set_hash",
            "proposal_binding_hash",
        ],
        [
            "evaluation_id",
            "proposal_id",
            "approval_set_hash",
            "proposal_binding_hash",
        ],
    )
    op.create_foreign_key(
        "fk_monthly_close_attempt_proposal_binding",
        "monthly_close_execution_attempts",
        "monthly_close_action_proposals",
        ["proposal_id", "proposal_binding_hash"],
        ["proposal_id", "proposal_binding_hash"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    op.drop_constraint(
        "fk_monthly_close_attempt_proposal_binding",
        "monthly_close_execution_attempts",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_monthly_close_attempt_evaluation_proposal",
        "monthly_close_execution_attempts",
        type_="foreignkey",
    )
    op.create_foreign_key(
        "fk_monthly_close_attempt_evaluation_proposal",
        "monthly_close_execution_attempts",
        "monthly_close_approval_evaluations",
        ["approval_evaluation_id", "proposal_id", "approval_set_hash"],
        ["evaluation_id", "proposal_id", "approval_set_hash"],
    )
    op.drop_constraint(
        "uq_monthly_close_evaluation_id_proposal_approval_binding",
        "monthly_close_approval_evaluations",
        type_="unique",
    )
    op.drop_constraint(
        "fk_monthly_close_evaluation_proposal_binding",
        "monthly_close_approval_evaluations",
        type_="foreignkey",
    )
    op.drop_constraint(
        "fk_monthly_close_approval_proposal_binding",
        "monthly_close_action_approvals",
        type_="foreignkey",
    )
    op.drop_constraint(
        "uq_monthly_close_proposal_id_binding",
        "monthly_close_action_proposals",
        type_="unique",
    )
    for table in reversed(TABLES):
        op.drop_column(table, "proposal_binding_hash")
