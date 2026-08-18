from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, func, or_
from typing import Optional

from app.core.deps import DBSession, CurrentUser
from app.models.guest import Guest
from app.models.order import Order
from app.schemas.guest import GuestOut, GuestUpdate

router = APIRouter(prefix="/guests", tags=["guests"])

# 客人档案含身份证/手机号明文与消费史。读=前台运营+财务，写=前台运营。
# 此前所有读端点只要登录即可访问，保洁/管家 token 也能整库拉身份证。
_GUEST_READ_ROLES = ("admin", "operator", "finance")
_GUEST_WRITE_ROLES = ("admin", "operator")


def _ensure_guest_access(current_user: dict, roles: tuple = _GUEST_READ_ROLES) -> None:
    if current_user["role"] not in roles:
        raise HTTPException(status_code=403, detail="无权访问客人档案")


@router.get("", response_model=list[GuestOut])
async def list_guests(
    db: DBSession,
    current_user: CurrentUser,
    keyword: Optional[str] = Query(default=None),
    tag: Optional[str] = Query(default=None),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List guests with optional search by name/phone and tag filter."""
    _ensure_guest_access(current_user)
    q = select(Guest)
    if keyword:
        q = q.where(
            or_(
                Guest.name.ilike(f"%{keyword}%"),
                Guest.phone.ilike(f"%{keyword}%"),
            )
        )
    if tag:
        q = q.where(Guest.tags.ilike(f"%{tag}%"))
    q = q.order_by(Guest.visit_count.desc(), Guest.updated_at.desc())
    q = q.offset((page - 1) * page_size).limit(page_size)
    result = await db.execute(q)
    return result.scalars().all()


@router.get("/lookup")
async def lookup_by_phone(
    phone: str,
    db: DBSession,
    current_user: CurrentUser,
):
    """Quick lookup by phone number — used in order creation for auto-fill."""
    _ensure_guest_access(current_user)
    result = await db.execute(select(Guest).where(Guest.phone == phone))
    guest = result.scalar_one_or_none()
    if not guest:
        return {"found": False}
    return {
        "found": True,
        "guest": GuestOut.model_validate(guest).model_dump(),
    }


@router.get("/{guest_id}", response_model=GuestOut)
async def get_guest(guest_id: str, db: DBSession, current_user: CurrentUser):
    _ensure_guest_access(current_user)
    result = await db.execute(select(Guest).where(Guest.guest_id == guest_id))
    guest = result.scalar_one_or_none()
    if not guest:
        raise HTTPException(status_code=404, detail="客人不存在")
    return guest


@router.get("/{guest_id}/orders")
async def get_guest_orders(guest_id: str, db: DBSession, current_user: CurrentUser):
    """Get all orders for a specific guest."""
    _ensure_guest_access(current_user)
    result = await db.execute(select(Guest).where(Guest.guest_id == guest_id))
    guest = result.scalar_one_or_none()
    if not guest:
        raise HTTPException(status_code=404, detail="客人不存在")

    orders_result = await db.execute(
        select(Order)
        .where(Order.guest_phone == guest.phone, Order.is_deleted == False)
        .order_by(Order.check_in_date.desc())
    )
    orders = orders_result.scalars().all()
    return [
        {
            "order_id": o.order_id,
            "room_id": o.room_id,
            "check_in_date": o.check_in_date.isoformat(),
            "check_out_date": o.check_out_date.isoformat(),
            "nights": o.nights,
            "actual_price": float(o.actual_price or 0),
            "order_status": o.order_status.value,
            "channel": o.channel.value,
            "created_at": o.created_at.isoformat() if o.created_at else None,
        }
        for o in orders
    ]


@router.patch("/{guest_id}", response_model=GuestOut)
async def update_guest(guest_id: str, body: GuestUpdate, db: DBSession, current_user: CurrentUser):
    _ensure_guest_access(current_user, _GUEST_WRITE_ROLES)
    result = await db.execute(select(Guest).where(Guest.guest_id == guest_id))
    guest = result.scalar_one_or_none()
    if not guest:
        raise HTTPException(status_code=404, detail="客人不存在")
    for field, value in body.model_dump(exclude_none=True).items():
        setattr(guest, field, value)
    await db.commit()
    await db.refresh(guest)
    return guest


@router.get("/stats/summary")
async def guest_summary(db: DBSession, current_user: CurrentUser):
    """Quick stats for guest overview."""
    _ensure_guest_access(current_user)
    total = await db.scalar(select(func.count()).select_from(Guest))
    repeat = await db.scalar(
        select(func.count()).select_from(Guest).where(Guest.visit_count > 1)
    )
    return {
        "total_guests": total or 0,
        "repeat_guests": repeat or 0,
        "repeat_rate": round((repeat or 0) / (total or 1) * 100, 1),
    }
