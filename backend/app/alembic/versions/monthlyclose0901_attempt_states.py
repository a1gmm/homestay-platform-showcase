"""align monthly-close execution attempt states with the control contract

Revision ID: monthlyclose0901attempts
Revises: monthlyclose0831reply
Create Date: 2026-09-01
"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op


revision: str = "monthlyclose0901attempts"
down_revision: Union[str, None] = "monthlyclose0831reply"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


NEW_STATES = (
    "pending",
    "executing",
    "succeeded_unverified",
    "verified",
    "failed_safe",
    "failed_confirmed",
    "unknown",
    "remediation_required",
)
OLD_STATES = (
    "queued",
    "executing",
    "succeeded",
    "failed_safe",
    "failed_unsafe",
    "unknown",
    "succeeded_confirmed",
    "failed_confirmed",
    "manual_remediation_required",
)


def _quoted(values: tuple[str, ...]) -> str:
    return ", ".join(f"'{value}'" for value in values)


def upgrade() -> None:
    op.drop_constraint(
        "ck_monthly_close_attempt_status",
        "monthly_close_execution_attempts",
        type_="check",
    )
    op.execute(
        sa.text(
            """
            UPDATE monthly_close_execution_attempts
            SET status = CASE status
                WHEN 'queued' THEN 'pending'
                WHEN 'succeeded' THEN 'succeeded_unverified'
                WHEN 'succeeded_confirmed' THEN 'verified'
                WHEN 'manual_remediation_required' THEN 'remediation_required'
                WHEN 'failed_unsafe' THEN 'remediation_required'
                ELSE status
            END
            """
        )
    )
    op.alter_column(
        "monthly_close_execution_attempts",
        "status",
        existing_type=sa.String(length=32),
        server_default="pending",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_monthly_close_attempt_status",
        "monthly_close_execution_attempts",
        f"status IN ({_quoted(NEW_STATES)})",
    )


def downgrade() -> None:
    op.drop_constraint(
        "ck_monthly_close_attempt_status",
        "monthly_close_execution_attempts",
        type_="check",
    )
    op.execute(
        sa.text(
            """
            UPDATE monthly_close_execution_attempts
            SET status = CASE status
                WHEN 'pending' THEN 'queued'
                WHEN 'succeeded_unverified' THEN 'succeeded'
                WHEN 'verified' THEN 'succeeded_confirmed'
                WHEN 'remediation_required' THEN 'manual_remediation_required'
                ELSE status
            END
            """
        )
    )
    op.alter_column(
        "monthly_close_execution_attempts",
        "status",
        existing_type=sa.String(length=32),
        server_default="queued",
        existing_nullable=False,
    )
    op.create_check_constraint(
        "ck_monthly_close_attempt_status",
        "monthly_close_execution_attempts",
        f"status IN ({_quoted(OLD_STATES)})",
    )
