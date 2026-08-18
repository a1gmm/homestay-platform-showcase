"""issue#3 guest_phone nullable

Revision ID: 2735db4afb5b
Revises: b56beadd972a
Create Date: 2026-05-09 22:30:20.481420

王总反馈："手机号一定不要弄成必填，前期订单来时往往没客人电话，
前台为了能录单只能瞎填，导致交接班时把假号当真号。"

把 orders.guest_phone 改为 NULLABLE。前端表单同步取消必填，但非空时仍校验
11 位中国大陆手机号格式（schemas/order.py 里的 _phone_format_when_present）。

services/guest_service.py::link_guest_to_order 已同步处理：phone 为空 →
跳过自动建客人档案，等前台补完电话后再人工 link。

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '2735db4afb5b'
down_revision: Union[str, None] = 'b56beadd972a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column(
        "orders",
        "guest_phone",
        existing_type=sa.String(20),
        nullable=True,
    )


def downgrade() -> None:
    # 注意：downgrade 前必须确保表中无 guest_phone IS NULL 的行，
    # 否则 alter 会失败。如需强制回退，先：
    #   UPDATE orders SET guest_phone = '00000000000' WHERE guest_phone IS NULL;
    op.alter_column(
        "orders",
        "guest_phone",
        existing_type=sa.String(20),
        nullable=False,
    )
