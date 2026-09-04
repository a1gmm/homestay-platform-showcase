"""make the room-overlap exclusion constraint deferrable

Revision ID: roomswap0902defer
Revises: cleaning0830cutoff
Create Date: 2026-09-02
"""

from typing import Sequence, Union

from alembic import op


revision: str = "roomswap0902defer"
down_revision: Union[str, None] = "cleaning0830cutoff"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_DROP_CONSTRAINT = """
ALTER TABLE orders
DROP CONSTRAINT IF EXISTS orders_no_room_overlap
"""

_CREATE_CONSTRAINT = """
ALTER TABLE orders
ADD CONSTRAINT orders_no_room_overlap
EXCLUDE USING gist (
    room_id WITH =,
    daterange(check_in_date, check_out_date, '[)') WITH &&
) WHERE (
    room_id IS NOT NULL
    AND is_deleted = false
    AND order_status NOT IN (
        'cancelled'::order_status,
        'completed'::order_status
    )
)
{deferrability}
"""


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute("CREATE EXTENSION IF NOT EXISTS btree_gist")
    op.execute(_DROP_CONSTRAINT)
    op.execute(
        _CREATE_CONSTRAINT.format(
            deferrability="DEFERRABLE INITIALLY IMMEDIATE",
        )
    )


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(_DROP_CONSTRAINT)
    op.execute(_CREATE_CONSTRAINT.format(deferrability="NOT DEFERRABLE"))

