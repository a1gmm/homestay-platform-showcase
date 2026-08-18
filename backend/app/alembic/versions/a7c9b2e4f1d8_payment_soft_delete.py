"""payment soft delete

Revision ID: a7c9b2e4f1d8
Revises: 925d967c7c54
Create Date: 2026-05-18 10:00:00.000000

王总反馈："现在只能收款不能删除了重新收款"。给 payments 表加软删字段，
后端新增 PATCH/DELETE /payments/{id} 路由，列表 + 实收聚合都过滤 is_deleted=True。
保留行做审计追溯。

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'a7c9b2e4f1d8'
down_revision: Union[str, None] = '925d967c7c54'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "payments",
        sa.Column(
            "is_deleted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "payments",
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "payments",
        sa.Column("deleted_by", sa.String(20), nullable=True),
    )
    op.create_foreign_key(
        "payments_deleted_by_fkey",
        "payments",
        "users",
        ["deleted_by"],
        ["user_id"],
    )


def downgrade() -> None:
    op.drop_constraint("payments_deleted_by_fkey", "payments", type_="foreignkey")
    op.drop_column("payments", "deleted_by")
    op.drop_column("payments", "deleted_at")
    op.drop_column("payments", "is_deleted")
