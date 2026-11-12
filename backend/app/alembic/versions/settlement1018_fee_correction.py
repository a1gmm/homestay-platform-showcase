"""Append-only owner self-stay cleaning corrections; no historical rewrites."""
from alembic import op
import sqlalchemy as sa

revision = 'settlement1018fees'
down_revision = 'monthlyclose1016support'
branch_labels = None
depends_on = None


def upgrade():
    op.execute("SET lock_timeout = '5s'")
    op.create_table('settlement_fee_adjustments',
        sa.Column('adjustment_id', sa.String(24), primary_key=True),
        sa.Column('settlement_id', sa.String(20), sa.ForeignKey('owner_settlements.settlement_id'), nullable=False),
        sa.Column('original_item_id', sa.String(20), sa.ForeignKey('owner_settlement_items.item_id'), nullable=False),
        sa.Column('entry_index', sa.Integer(), nullable=False),
        sa.Column('original_expense_id', sa.String(20), sa.ForeignKey('expenses.expense_id'), nullable=False),
        sa.Column('replacement_expense_id', sa.String(20), sa.ForeignKey('expenses.expense_id'), nullable=False),
        sa.Column('before_amount', sa.Numeric(10,2), nullable=False),
        sa.Column('after_amount', sa.Numeric(10,2), nullable=False),
        sa.Column('proposal_id', sa.String(24), sa.ForeignKey('financial_case_proposals.proposal_id'), nullable=False),
        sa.Column('actor_id', sa.String(20), sa.ForeignKey('users.user_id'), nullable=False),
        sa.Column('reason', sa.Text(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.UniqueConstraint('original_expense_id', name='uq_settlement_fee_original'),
        sa.UniqueConstraint('replacement_expense_id', name='uq_settlement_fee_replacement'),
        sa.CheckConstraint('before_amount = 30 AND after_amount = 65', name='ck_self_stay_fee_correction'))
    op.add_column('owner_settlement_items', sa.Column('fee_adjustment_id', sa.String(24)))
    op.create_foreign_key('fk_settlement_item_fee_adjustment', 'owner_settlement_items', 'settlement_fee_adjustments', ['fee_adjustment_id'], ['adjustment_id'])
    op.create_unique_constraint('uq_settlement_item_fee_adjustment', 'owner_settlement_items', ['fee_adjustment_id'])
    op.execute("""CREATE FUNCTION reject_settlement_fee_adjustment_mutation() RETURNS trigger AS $$
        BEGIN RAISE EXCEPTION 'settlement fee correction evidence is append-only'; END; $$ LANGUAGE plpgsql""")
    op.execute('CREATE TRIGGER settlement_fee_adjustment_immutable BEFORE UPDATE OR DELETE ON settlement_fee_adjustments FOR EACH ROW EXECUTE FUNCTION reject_settlement_fee_adjustment_mutation()')
    op.execute("""CREATE TRIGGER settlement_fee_line_immutable BEFORE UPDATE OR DELETE ON owner_settlement_items
        FOR EACH ROW WHEN (OLD.fee_adjustment_id IS NOT NULL) EXECUTE FUNCTION reject_settlement_fee_adjustment_mutation()""")


def downgrade():
    # Evidence cannot be discarded by an accidental downgrade.
    op.execute("DO $$ BEGIN IF EXISTS (SELECT 1 FROM settlement_fee_adjustments) THEN RAISE EXCEPTION 'fee correction evidence exists'; END IF; END $$")
    op.execute('DROP TRIGGER settlement_fee_line_immutable ON owner_settlement_items')
    op.execute('DROP TRIGGER settlement_fee_adjustment_immutable ON settlement_fee_adjustments')
    op.execute('DROP FUNCTION reject_settlement_fee_adjustment_mutation()')
    op.drop_constraint('uq_settlement_item_fee_adjustment', 'owner_settlement_items', type_='unique')
    op.drop_constraint('fk_settlement_item_fee_adjustment', 'owner_settlement_items', type_='foreignkey')
    op.drop_column('owner_settlement_items', 'fee_adjustment_id')
    op.drop_table('settlement_fee_adjustments')
