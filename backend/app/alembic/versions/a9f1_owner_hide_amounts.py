"""owner: view_as_owner_id + hide_amounts + hide_guests（脱敏镜像业主账号）

给 owners 加三列：
- view_as_owner_id：镜像视图账号看哪个真业主的房间/数据（演示号）。自引用 FK。
- hide_amounts：True 时业主端所有接口把金额后端置 None（真数字不出后端）。
- hide_guests：True 时把订单里的客人姓名后端脱敏为「客人」（演示号不露真实客人信息）。

Revision ID: a9f1_owner_hide_amounts
Revises: f1a2b3c4d5e6
Create Date: 2026-07-24
"""
from alembic import op
import sqlalchemy as sa


# 可读且唯一的 revision id（避开手编 hex 撞号）
revision = "a9f1_owner_hide_amounts"
down_revision = "f1a2b3c4d5e6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "owners",
        sa.Column("view_as_owner_id", sa.String(length=20), nullable=True),
    )
    op.add_column(
        "owners",
        sa.Column(
            "hide_amounts",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.add_column(
        "owners",
        sa.Column(
            "hide_guests",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.create_foreign_key(
        "fk_owners_view_as_owner_id",
        "owners",
        "owners",
        ["view_as_owner_id"],
        ["owner_id"],
    )


def downgrade() -> None:
    op.drop_constraint("fk_owners_view_as_owner_id", "owners", type_="foreignkey")
    op.drop_column("owners", "hide_guests")
    op.drop_column("owners", "hide_amounts")
    op.drop_column("owners", "view_as_owner_id")
