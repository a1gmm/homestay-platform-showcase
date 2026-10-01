"""Add source evidence and version-bound case proposals.

Revision ID: financialcase0907
Revises: cleaning0907chatremovals
"""
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from alembic import op
revision = 'financialcase0907'
down_revision = 'cleaning0907chatremovals'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('financial_case_sources',
        sa.Column('source_id', sa.String(24), primary_key=True),
        sa.Column('cycle_id', sa.String(24), sa.ForeignKey('monthly_close_cycles.cycle_id'), nullable=False),
        sa.Column('filename', sa.String(255), nullable=False),
        sa.Column('sha256', sa.String(64), nullable=False),
        sa.Column('kind', sa.String(24), nullable=False),
        sa.Column('content', sa.LargeBinary(), nullable=False),
        sa.Column('parsed', JSONB(), nullable=False),
        sa.Column('decisions', JSONB(), nullable=False),
        sa.Column('version', sa.Integer(), nullable=False),
        sa.Column('document_id', sa.String(24), sa.ForeignKey('monthly_close_documents.document_id')),
        sa.Column('created_by', sa.String(20), sa.ForeignKey('users.user_id'), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint('cycle_id', 'sha256', name='uq_financial_case_source_digest'))
    op.create_index('ix_financial_case_sources_cycle_id', 'financial_case_sources', ['cycle_id'])
    op.create_table('financial_case_proposals',
        sa.Column('proposal_id', sa.String(24), primary_key=True),
        sa.Column('cycle_id', sa.String(24), sa.ForeignKey('monthly_close_cycles.cycle_id'), nullable=False),
        sa.Column('run_id', sa.String(24), sa.ForeignKey('monthly_close_agent_runs.run_id'), nullable=False, unique=True),
        sa.Column('created_by', sa.String(20), sa.ForeignKey('users.user_id'), nullable=False),
        sa.Column('snapshot_hash', sa.String(64), nullable=False),
        sa.Column('payload', JSONB(), nullable=False),
        sa.Column('status', sa.String(24), nullable=False),
        sa.Column('result', JSONB()),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False))
    op.create_index('ix_financial_case_proposals_cycle_id', 'financial_case_proposals', ['cycle_id'])


def downgrade():
    op.drop_table('financial_case_proposals')
    op.drop_table('financial_case_sources')
