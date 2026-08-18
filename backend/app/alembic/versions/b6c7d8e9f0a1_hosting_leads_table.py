"""hosting_leads table (业主托管留资，tuoguan landing page)

Revision ID: b6c7d8e9f0a1
Revises: a7c1e2f3b4d5
Create Date: 2026-06-11 00:00:00.000000

观海居托管招商落地页的留资线索表。IF NOT EXISTS 守卫：与本仓库
「alembic 基线为空 + lifespan DDL 并存」的历史现状兼容（见 docs 审计），
重复执行/已有表时安全。
"""
from typing import Sequence, Union

from alembic import op

revision: str = "b6c7d8e9f0a1"
down_revision: Union[str, None] = "a7c1e2f3b4d5"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS hosting_leads (
            lead_id VARCHAR(20) PRIMARY KEY,
            name VARCHAR(50) NOT NULL,
            phone VARCHAR(20) NOT NULL,
            property_location VARCHAR(200) NOT NULL,
            source_ua TEXT,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now()
        )
        """
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_hosting_leads_phone_created"
        " ON hosting_leads(phone, created_at)"
    )
    # flood 护栏的全局 count 只按 created_at 过滤，复合索引（phone 前导）用不上
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_hosting_leads_created_at"
        " ON hosting_leads(created_at)"
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS hosting_leads")
