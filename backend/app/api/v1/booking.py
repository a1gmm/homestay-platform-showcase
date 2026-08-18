"""
C-side (customer-facing) booking API.

Public (no auth):
- GET  /booking/rooms                      — list bookable rooms (optional date filter)
- GET  /booking/rooms/{id}                 — room detail
- GET  /booking/rooms/{id}/calendar        — per-day availability + price for a date range

Customer-auth required:
- POST /booking/orders                     — create a booking (channel=direct)
"""
from fastapi import APIRouter, HTTPException, Query, Request
from sqlalchemy import select
from datetime import date, timedelta
from decimal import Decimal
from typing import Optional
from app.core.deps import DBSession, CurrentCustomerPhone, RedisClient
from app.core.ids import gen_order_room_id, unique_order_id
from app.core.rate_limit import enforce_rate_limit
from app.models.room import Room, RoomStatus
from app.models.order import Order, OrderStatus, Channel, PaymentStatus
from app.models.room_block import RoomBlock
from app.models.order_room import OrderRoom
from app.models.customer import Customer
from app.services.order_pricing import compute_daily_prices
from app.schemas.booking import (
    RoomPublic, CalendarDay, BookingOrderCreate, BookingOrderOut,
)
from app.services.room_availability import check_room_conflict
from app.services.audit import log_action_tx

router = APIRouter(prefix="/booking", tags=["booking"])


# ─── Public: rooms list ──────────────────────────────────────────────────────

@router.get("/rooms", response_model=list[RoomPublic])
async def list_public_rooms(
    db: DBSession,
    check_in: Optional[date] = Query(default=None),
    check_out: Optional[date] = Query(default=None),
):
    """
    列出可订房源。
    - 如果传了 check_in/check_out，过滤掉这个日期段已被订/封锁的房间。
    - 默认排除 maintenance/locked 状态的房间。
    """
    rooms_q = select(Room).where(
        Room.is_deleted == False,
        Room.room_status.not_in([RoomStatus.maintenance, RoomStatus.locked]),
    ).order_by(Room.room_id)
    rooms = (await db.execute(rooms_q)).scalars().all()

    if check_in and check_out and check_out > check_in:
        # 查所有冲突订单和房屋封锁
        conflicts = await _get_conflicting_room_ids(db, check_in, check_out)
        rooms = [r for r in rooms if r.room_id not in conflicts]

    images_by_room = await _fetch_images_by_room(db, [r.room_id for r in rooms])
    return [RoomPublic.from_room(r, images=images_by_room.get(r.room_id, [])) for r in rooms]


async def _fetch_images_by_room(db, room_ids: list[str]) -> dict[str, list[str]]:
    """Return {room_id: [url1, url2, ...]} with cover first, rest by sort_order."""
    if not room_ids:
        return {}
    from app.models.room_image import RoomImage

    rows = (
        await db.execute(
            select(RoomImage)
            .where(RoomImage.room_id.in_(room_ids))
            .order_by(
                RoomImage.is_cover.desc(),  # cover first
                RoomImage.sort_order.asc(),
                RoomImage.created_at.asc(),
            )
        )
    ).scalars().all()
    out: dict[str, list[str]] = {}
    for img in rows:
        out.setdefault(img.room_id, []).append(img.url)
    return out


async def _get_conflicting_room_ids(db, check_in: date, check_out: date) -> set[str]:
    # Multi-room: 查 order_rooms 表，按多房订单的每行独立检测冲突
    from app.models.order_room import OrderRoom
    order_q = (
        select(OrderRoom.room_id)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .where(
            OrderRoom.room_id.isnot(None),
            Order.is_deleted == False,
            Order.order_status.not_in([OrderStatus.cancelled]),
            OrderRoom.check_in_date < check_out,
            OrderRoom.check_out_date > check_in,
        )
    )
    block_q = (
        select(RoomBlock.room_id)
        .where(
            RoomBlock.start_date < check_out,
            RoomBlock.end_date >= check_in,
        )
    )
    order_ids = {r[0] for r in (await db.execute(order_q)).all() if r[0]}
    block_ids = {r[0] for r in (await db.execute(block_q)).all() if r[0]}
    return order_ids | block_ids


# ─── Public: room detail ─────────────────────────────────────────────────────

@router.get("/rooms/{room_id}", response_model=RoomPublic)
async def get_public_room(room_id: str, db: DBSession):
    result = await db.execute(select(Room).where(Room.room_id == room_id))
    room = result.scalar_one_or_none()
    if not room or room.room_status in (RoomStatus.maintenance, RoomStatus.locked):
        raise HTTPException(status_code=404, detail="房源不存在或暂不可订")
    images = (await _fetch_images_by_room(db, [room_id])).get(room_id, [])
    return RoomPublic.from_room(room, images=images)


# ─── Public: room calendar (availability + price by day) ─────────────────────

@router.get("/rooms/{room_id}/calendar", response_model=list[CalendarDay])
async def room_calendar(
    room_id: str,
    db: DBSession,
    start: date = Query(...),
    end: date = Query(...),
):
    if end <= start:
        raise HTTPException(status_code=400, detail="end 必须晚于 start")
    if (end - start).days > 90:
        raise HTTPException(status_code=400, detail="日期范围不能超过 90 天")

    room = (await db.execute(select(Room).where(Room.room_id == room_id))).scalar_one_or_none()
    if not room:
        raise HTTPException(status_code=404, detail="房源不存在")

    # 收集该房间所有冲突日
    busy_days: set[date] = set()

    # 按 order_rooms 查（多房间真相源），避免漏读非首间房 / 把整单跨度涂到单房 (#39)
    order_rooms = (await db.execute(
        select(OrderRoom)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .where(
            OrderRoom.room_id == room_id,
            Order.is_deleted == False,
            Order.order_status.not_in([OrderStatus.cancelled]),
            OrderRoom.check_in_date < end,
            OrderRoom.check_out_date > start,
        )
    )).scalars().all()
    for orr in order_rooms:
        cur = max(orr.check_in_date, start)
        stop = min(orr.check_out_date, end)
        while cur < stop:
            busy_days.add(cur)
            cur += timedelta(days=1)

    blocks = (await db.execute(
        select(RoomBlock).where(
            RoomBlock.room_id == room_id,
            RoomBlock.start_date < end,
            RoomBlock.end_date >= start,
        )
    )).scalars().all()
    for b in blocks:
        cur = max(b.start_date, start)
        stop = min(b.end_date + timedelta(days=1), end)
        while cur < stop:
            busy_days.add(cur)
            cur += timedelta(days=1)

    # 构造日历
    days: list[CalendarDay] = []
    cur = start
    base = room.base_price
    weekend_markup = room.weekend_markup or Decimal("0")
    while cur < end:
        price = base
        if base is not None and cur.weekday() in (4, 5) and weekend_markup:
            # 周五、周六按 weekend_markup
            price = (base * (Decimal("1") + weekend_markup / Decimal("100"))).quantize(Decimal("0.01"))
        days.append(CalendarDay(
            date=cur,
            price=price,
            available=cur not in busy_days,
        ))
        cur += timedelta(days=1)
    return days


# ─── Customer-auth: create booking ───────────────────────────────────────────

def _calc_price(room: Room, check_in: date, check_out: date) -> tuple[Decimal | None, int]:
    """Returns (total_price, nights). None if room has no base_price."""
    nights = (check_out - check_in).days
    if not room.base_price:
        return None, nights
    total = Decimal("0")
    weekend_markup = room.weekend_markup or Decimal("0")
    cur = check_in
    while cur < check_out:
        day_price = room.base_price
        if cur.weekday() in (4, 5) and weekend_markup:
            day_price = (room.base_price * (Decimal("1") + weekend_markup / Decimal("100"))).quantize(Decimal("0.01"))
        total += day_price
        cur += timedelta(days=1)
    return total.quantize(Decimal("0.01")), nights


@router.post("/orders", response_model=BookingOrderOut, status_code=201)
async def create_booking(
    body: BookingOrderCreate,
    phone: CurrentCustomerPhone,
    db: DBSession,
    redis: RedisClient,
    request: Request,
):
    # 防刷：同一手机+IP 1分钟内最多 10 单
    ip = request.client.host if request.client else "unknown"
    await enforce_rate_limit(
        redis, f"booking_create:{phone}:{ip}", limit=10, window_seconds=60,
        detail="下单过于频繁，请稍后再试",
    )

    # 1) 房间存在 + 可订
    room = (await db.execute(select(Room).where(Room.room_id == body.room_id))).scalar_one_or_none()
    if not room or room.room_status in (RoomStatus.maintenance, RoomStatus.locked):
        raise HTTPException(status_code=404, detail="房源不存在或暂不可订")

    # 2) 最短住宿
    nights = (body.check_out_date - body.check_in_date).days
    if nights < (room.min_stay_nights or 1):
        raise HTTPException(status_code=400, detail=f"该房源最少需住 {room.min_stay_nights} 晚")

    # 3) 并发安全的可用性检查（SELECT FOR UPDATE 已在 check_room_conflict 内）
    has_conflict = await check_room_conflict(
        db, body.room_id, body.check_in_date, body.check_out_date
    )
    if has_conflict:
        raise HTTPException(status_code=409, detail="该日期段已被预订")

    # 4) 再检查房屋封锁（owner_use/maintenance 等非订单冲突）
    block = (await db.execute(
        select(RoomBlock).where(
            RoomBlock.room_id == body.room_id,
            RoomBlock.start_date < body.check_out_date,
            RoomBlock.end_date >= body.check_in_date,
        ).limit(1)
    )).scalar_one_or_none()
    if block:
        raise HTTPException(status_code=409, detail="该日期段不可订")

    # 5) 计算价格
    total_price, _ = _calc_price(room, body.check_in_date, body.check_out_date)

    # 6) 确保 Customer 记录存在并同步 name/id_number
    customer = (await db.execute(select(Customer).where(Customer.phone == phone))).scalar_one_or_none()
    if customer is None:
        # 异常情况：JWT 合法但 Customer 记录缺失，重新建一条
        customer = Customer(phone=phone, name=body.guest_name, id_number=body.id_number)
        db.add(customer)
    else:
        if body.id_number and not customer.id_number:
            customer.id_number = body.id_number

    # 7) 创建订单 + 子房行
    #    多房间改造后，所有冲突检查/可订查询/按房财务/房东结算都只查 order_rooms，
    #    所以 C 端订单必须同步写入一条 OrderRoom，否则对防双订机制完全不可见 (#39)。
    order = Order(
        order_id=await unique_order_id(db, prefix="C"),
        channel=Channel.self_acquired,
        guest_name=body.guest_name,
        guest_phone=phone,
        room_id=body.room_id,
        check_in_date=body.check_in_date,
        check_out_date=body.check_out_date,
        list_price=total_price,
        actual_price=total_price,
        order_status=OrderStatus.pending_confirm,
        payment_status=PaymentStatus.unpaid,
        notes=body.notes,
        created_by=None,  # C-side 下单没有 admin user
    )
    order.rooms.append(OrderRoom(
        order_room_id=gen_order_room_id(),
        room_id=body.room_id,
        check_in_date=body.check_in_date,
        check_out_date=body.check_out_date,
        list_price=total_price,
        actual_price=total_price,
        guests_count=0,
        position=0,
        daily_prices=compute_daily_prices(
            body.check_in_date, body.check_out_date, total_price
        ),
    ))
    db.add(order)
    await log_action_tx(
        db, None, "booking.create", "order", order.order_id,
        after_data={"order_id": order.order_id, "guest_phone": phone, "room_id": body.room_id},
        notes="C-side direct booking",
    )
    await db.commit()
    await db.refresh(order)

    return BookingOrderOut(
        order_id=order.order_id,
        room_id=order.room_id,
        check_in_date=order.check_in_date,
        check_out_date=order.check_out_date,
        nights=order.nights,
        guest_name=order.guest_name,
        guest_phone=order.guest_phone,
        list_price=order.list_price,
        actual_price=order.actual_price,
        order_status=order.order_status.value,
        payment_status=order.payment_status.value,
        created_at=order.created_at.isoformat() if order.created_at else "",
    )
