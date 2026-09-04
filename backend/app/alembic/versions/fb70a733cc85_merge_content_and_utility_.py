"""merge content and utility reconciliation heads

Revision ID: fb70a733cc85
Revises: miniapp0822c1, utility_recon_0822
Create Date: 2026-08-22 06:08:02.059376

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'fb70a733cc85'
down_revision: Union[str, None] = ('miniapp0822c1', 'utility_recon_0822')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
