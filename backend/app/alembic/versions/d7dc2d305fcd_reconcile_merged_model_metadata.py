"""reconcile merged model metadata

Revision ID: d7dc2d305fcd
Revises: fb70a733cc85
Create Date: 2026-08-22 06:24:37.609639

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7dc2d305fcd'
down_revision: Union[str, None] = 'fb70a733cc85'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # The frozen compatibility snapshot used PostgreSQL's generated FK name,
    # while historical upgraded databases use our explicit Alembic name.
    # Normalize either shape after the two migration branches merge.
    op.execute("ALTER TABLE owners DROP CONSTRAINT IF EXISTS owners_parent_owner_id_fkey")
    op.execute("ALTER TABLE owners DROP CONSTRAINT IF EXISTS fk_owners_parent_owner_id")
    op.create_foreign_key(
        "fk_owners_parent_owner_id",
        "owners",
        "owners",
        ["parent_owner_id"],
        ["owner_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_owners_parent_owner_id", "owners", type_="foreignkey")
    op.create_foreign_key(
        "owners_parent_owner_id_fkey",
        "owners",
        "owners",
        ["parent_owner_id"],
        ["owner_id"],
    )
