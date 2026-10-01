"""Freeze readable income details when an owner statement is confirmed."""
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
from alembic import op

revision = 'settlement0910details'
down_revision = 'financialcase0907fees'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET lock_timeout = '5s'")
    op.add_column('owner_settlements', sa.Column('income_detail_snapshot', postgresql.JSONB(), nullable=True))


def downgrade():
    op.drop_column('owner_settlements', 'income_detail_snapshot')
