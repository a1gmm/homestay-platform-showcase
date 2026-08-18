"""owner parent account

Revision ID: da25ebecffd5
Revises: a1b2c3d4e5f6
Create Date: 2026-07-07 02:34:32.105017

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'da25ebecffd5'
down_revision: Union[str, None] = 'a1b2c3d4e5f6'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("owners", sa.Column("parent_owner_id", sa.String(length=20), nullable=True))
    op.create_foreign_key(
        "fk_owners_parent_owner_id",
        "owners", "owners",
        ["parent_owner_id"], ["owner_id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("fk_owners_parent_owner_id", "owners", type_="foreignkey")
    op.drop_column("owners", "parent_owner_id")
