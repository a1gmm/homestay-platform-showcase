"""Add explicit operating expense categories without changing owner cost shares."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = 'financialcase0907fees'
down_revision = 'financialcase0907'
branch_labels = None
depends_on = None


def upgrade():
    with op.get_context().autocommit_block():
        for value in ('payroll', 'social_insurance', 'bank_fee', 'rent', 'operating_expense',
                      'cleaning_supplier_cost', 'laundry_supplier_cost'):
            op.execute(f"ALTER TYPE expense_category ADD VALUE IF NOT EXISTS '{value}'")
    op.add_column('expenses', sa.Column('payment_date', sa.Date(), nullable=True))
    op.add_column('expenses', sa.Column('paid_by', postgresql.ENUM(
        'company', 'owner', name='expense_payer', create_type=False), nullable=True))


def downgrade():
    op.drop_column('expenses', 'paid_by')
    op.drop_column('expenses', 'payment_date')
    # Keep additive enum values: historical rows may still use them.
