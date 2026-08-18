"""merge smart-lock into main

Revision ID: e6031ed97daf
Revises: 5c0b1a2d3e4f, da25ebecffd5
Create Date: 2026-07-08 00:09:28.273359

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e6031ed97daf'
down_revision: Union[str, None] = ('5c0b1a2d3e4f', 'da25ebecffd5')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
