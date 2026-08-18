"""Feature C: Global search across orders, guests, and rooms."""

from fastapi import APIRouter, Query, HTTPException
from sqlalchemy import select, or_
from typing import Optional

from app.core.deps import DBSession, CurrentUser
from app.models.order import Order
from app.models.guest import Guest
from app.models.room import Room

router = APIRouter(prefix="/search", tags=["search"])


@router.get("")
async def global_search(
    db: DBSession,
    current_user: CurrentUser,
    q: str = Query(..., min_length=1, max_length=100, description="搜索关键词"),
):
    """Search across orders, guests, and rooms."""
    # 搜索结果含客人姓名/手机号（orders + guests 两段），与 /guests 同级敏感。
    # 不收权的话，保洁/管家 token 可以用前缀遍历（q=13, q=130…）把客人档案
    # 整库拉出来，/guests 的角色限制形同虚设。
    if current_user["role"] not in ("admin", "operator", "finance"):
        raise HTTPException(status_code=403, detail="无权使用全局搜索")

    keyword = f"%{q}%"

    # Search orders (guest_name, guest_phone, order_id)
    orders_result = await db.execute(
        select(Order).where(
            Order.is_deleted == False,
            or_(
                Order.guest_name.ilike(keyword),
                Order.guest_phone.ilike(keyword),
                Order.order_id.ilike(keyword),
            ),
        ).order_by(Order.created_at.desc()).limit(10)
    )
    orders = orders_result.scalars().all()

    # Search guests (name, phone)
    guests_result = await db.execute(
        select(Guest).where(
            or_(
                Guest.name.ilike(keyword),
                Guest.phone.ilike(keyword),
            )
        ).limit(10)
    )
    guests = guests_result.scalars().all()

    # Search rooms (room_id, room_name)
    rooms_result = await db.execute(
        select(Room).where(
            Room.is_deleted == False,
            or_(
                Room.room_id.ilike(keyword),
                Room.room_name.ilike(keyword),
            ),
        ).limit(10)
    )
    rooms = rooms_result.scalars().all()

    return {
        "orders": [
            {
                "order_id": o.order_id,
                "guest_name": o.guest_name,
                "guest_phone": o.guest_phone,
                "room_id": o.room_id,
                "check_in_date": str(o.check_in_date),
                "check_out_date": str(o.check_out_date),
                "order_status": o.order_status.value,
            }
            for o in orders
        ],
        "guests": [
            {
                "guest_id": g.guest_id,
                "name": g.name,
                "phone": g.phone,
                "visit_count": g.visit_count,
            }
            for g in guests
        ],
        "rooms": [
            {
                "room_id": r.room_id,
                "room_name": r.room_name,
                "room_status": r.room_status.value,
            }
            for r in rooms
        ],
    }
