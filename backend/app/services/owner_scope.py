"""业主作用域计算：总账号（有子业主的 owner）登录时能看 自己 + 直属子业主。"""
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.owner import Owner


async def get_scoped_owner_ids(db: AsyncSession, owner_id: str) -> list[str]:
    """返回 [owner_id] 加上其直属子业主的 owner_id（单层）。

    普通/子业主没有子业主 → 只返回 [owner_id]，保持既有行为。
    """
    rows = await db.execute(
        select(Owner.owner_id).where(Owner.parent_owner_id == owner_id)
    )
    ids = [owner_id]
    ids.extend(rows.scalars().all())
    return ids


async def get_sub_owners(db: AsyncSession, owner_id: str) -> list[Owner]:
    """直属子业主对象列表，按 name 排序（供总账号 profile / 分组展示）。"""
    rows = await db.execute(
        select(Owner).where(Owner.parent_owner_id == owner_id).order_by(Owner.name)
    )
    return list(rows.scalars().all())
