"""
Room conflict detection.

Concurrency model: lock the Room row first (SELECT FOR UPDATE on the rooms
table). All bookings for that room are then serialised — concurrent requests
queue at the row lock. This is robust regardless of whether the orders table
has matching rows yet (which is the case the previous SELECT FOR UPDATE on
orders silently bypassed).

For belt-and-suspenders DB-level protection, a partial GIST EXCLUDE constraint
is provided in `backend/scripts/install_order_overlap_constraint.sql` —
run it once on Neon to make double-booking provably impossible at the schema
level.
"""
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from datetime import date, timedelta
from typing import Optional

from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.room import Room
from app.models.room_block import RoomBlock


async def check_room_conflict(
    db: AsyncSession,
    room_id: str,
    check_in: date,
    check_out: date,
    exclude_order_id: Optional[str] = None,
    exclude_order_room_id: Optional[str] = None,
    exclude_stay_group_id: Optional[str] = None,
) -> bool:
    """Returns True if there is a booking conflict for the given room/dates.

    检测两种冲突源：
    1. 已存在的 OrderRoom（多房订单的某行）— 排除已取消订单和指定 exclude
    2. 锁房记录（RoomBlock）— owner_use / maintenance / reserved / other

    exclude_order_id: 排除整张订单（update_order diff 阶段用）
    exclude_order_room_id: 排除某一具体 order_room 行（单房调整时用，避免自冲突）

    任一命中即 True。前台/管家关房后排单会被拦截。
    """
    # 锁住 Room 行 — 并发请求在这里排队，比 SELECT FOR UPDATE on order_rooms 更可靠
    # （order_rooms 表可能尚无冲突行可锁）。Room 不存在时不需要锁。
    # 顺便读软删标记：已下线（或不存在）的房间一律视为不可订，挡住排房/换房/建单。
    # 区分「行不存在」与「is_deleted 为 falsy」：存量房若因迁移未回填而 is_deleted=NULL，
    # 仍应视为未删放行，绝不能因 NULL 把所有存量房判成不可订。
    room_row = (await db.execute(
        select(Room.room_id, Room.is_deleted)
        .where(Room.room_id == room_id).with_for_update()
    )).first()
    if room_row is None:       # 房间不存在
        return True
    if room_row.is_deleted:    # 已下线（NULL / False 均放行）
        return True

    # 主冲突查询：join order_rooms × orders，排除已取消订单
    q = (
        select(OrderRoom.order_room_id)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .where(
            OrderRoom.room_id == room_id,
            Order.is_deleted == False,
            Order.order_status.not_in([OrderStatus.cancelled]),
            OrderRoom.check_in_date < check_out,
            OrderRoom.check_out_date > check_in,
        )
        .limit(1)
    )

    if exclude_order_id:
        q = q.where(OrderRoom.order_id != exclude_order_id)
    if exclude_order_room_id:
        q = q.where(OrderRoom.order_room_id != exclude_order_room_id)
    if exclude_stay_group_id:
        # SQL ``NULL != value`` is UNKNOWN, so preserve ungrouped external
        # orders explicitly while excluding every member of the current group.
        q = q.where(or_(
            Order.stay_group_id.is_(None),
            Order.stay_group_id != exclude_stay_group_id,
        ))

    order_result = await db.execute(q)
    if order_result.scalar_one_or_none() is not None:
        return True

    # 检测锁房记录冲突。半开区间 [start_date, end_date) 与订单 [check_in, check_out) 一致语义。
    block_result = await db.execute(
        select(RoomBlock.block_id)
        .where(
            RoomBlock.room_id == room_id,
            RoomBlock.start_date < check_out,
            RoomBlock.end_date > check_in,
        )
        .limit(1)
    )
    if block_result.scalar_one_or_none() is not None:
        return True

    # Room remains locked from the start of this function. Re-read arrival
    # evidence under that lock before placing another guest into an overdue
    # stay today; planned checkout-day turnover and future bookings keep the
    # normal half-open calendar rules above.
    from app.core.datetime_helpers import today_cn
    from app.services.room_presence import current_room_presence
    today = today_cn()
    if check_in <= today < check_out:
        present = await current_room_presence(
            db, today, room_id=room_id, exclude_order_id=exclude_order_id,
            exclude_order_room_id=exclude_order_room_id,
            exclude_stay_group_id=exclude_stay_group_id,
        )
        return any(room.checkout_date < today for room in present)
    return False


async def describe_room_conflict(
    db: AsyncSession, room_id: str, check_in: date, check_out: date,
    exclude_order_id: str | None = None,
) -> dict | None:
    """Read-only explanation for the selected room; write guards keep their row lock.

    Check actual overdue presence first: the old booking can be outside the visible
    calendar window. Reuse the physical-leg resolver so checked-out continuation
    tails cannot resurrect an old anchor. No guest contact details are returned.
    """
    from app.core.datetime_helpers import today_cn
    from app.services.room_presence import current_room_presence
    from app.models.room import RoomStatus

    room = await db.get(Room, room_id)
    if room is None or room.is_deleted:
        return {"kind": "unavailable", "message": f"房间 {room_id} 已下线或不存在，请重新选择房间。"}
    today = today_cn()
    if check_in <= today < check_out:
        present = await current_room_presence(db, today, room_id=room_id,
                                              exclude_order_id=exclude_order_id)
        overdue = next((p for p in present if p.checkout_date < today), None)
        if overdue:
            return {
                "kind": "overdue", "order_id": overdue.order_id,
                "message": f"房间 {room_id} 有超期在住订单（{overdue.order_id}），预计 {overdue.checkout_date} 退房，尚未办理退房。请核实客人是否离店：已离店请办理退房，仍在住请处理续住或选择其他房间。",
            }
    q = (select(OrderRoom).join(Order, Order.order_id == OrderRoom.order_id)
         .where(OrderRoom.room_id == room_id, Order.is_deleted == False,
                Order.order_status != OrderStatus.cancelled,
                OrderRoom.check_in_date < check_out, OrderRoom.check_out_date > check_in)
         .order_by(OrderRoom.check_in_date, OrderRoom.order_room_id).limit(1))
    if exclude_order_id:
        q = q.where(OrderRoom.order_id != exclude_order_id)
    occupied = (await db.execute(q)).scalar_one_or_none()
    if occupied:
        return {
            "kind": "order", "order_id": occupied.order_id,
            "message": f"房间 {room_id} 的日期与订单 {occupied.order_id} 冲突：该订单 {occupied.check_in_date} 入住、{occupied.check_out_date} 退房（退房当天不占夜）。请查看原订单或更换房间、日期。",
        }
    block = (await db.execute(select(RoomBlock).where(
        RoomBlock.room_id == room_id, RoomBlock.start_date < check_out,
        RoomBlock.end_date > check_in,
    ).order_by(RoomBlock.start_date, RoomBlock.block_id).limit(1))).scalar_one_or_none()
    if block:
        labels = {"maintenance": "维修", "owner_use": "业主自用", "reserved": "预留", "other": "其他"}
        kind = getattr(block.block_type, "value", block.block_type)
        return {
            "kind": "block", "block_id": block.block_id,
            "message": f"房间 {room_id} 在 {block.start_date} 至 {block.end_date} 已锁房（{labels.get(kind, kind)}；结束日不占夜）" + (f"：{block.reason}" if block.reason else "") + "。请核实锁房安排或选择其他房间。",
        }
    if room.room_status in (RoomStatus.maintenance, RoomStatus.locked):
        label = "维修" if room.room_status == RoomStatus.maintenance else "锁房"
        return {"kind": "unavailable", "message": f"房间 {room_id} 当前为{label}状态，请核实并恢复房态后再选房。"}
    return None


async def get_available_rooms(
    db: AsyncSession,
    check_in: date,
    check_out: date,
    exclude_order_id: str | None = None,
) -> list[str]:
    """返回 [check_in, check_out) 窗口内可订房的 room_id。

    可用性单一定义：
      - 静态 maintenance/locked 排除（无限期硬锁；按日期锁请用 RoomBlock）
      - 与窗口重叠的 active 订单 → 排除
      - 与窗口重叠的 RoomBlock → 排除
      - 窗口覆盖今天且实际在住已超期未退房 → 排除
    静态 occupied/reserved/pending_clean/cleaning 不参与判断。

    exclude_order_id：编辑订单场景传本单 id，本单自己的占用不算冲突
    （否则编辑弹窗里本单已占的房会被误禁选）。
    """
    from app.models.room import Room, RoomStatus

    all_rooms_result = await db.execute(
        select(Room.room_id).where(
            Room.is_deleted == False,
            Room.room_status.not_in([RoomStatus.maintenance, RoomStatus.locked]),
        )
    )
    all_room_ids = {r[0] for r in all_rooms_result.all()}

    order_conflict_conds = [
        OrderRoom.room_id.isnot(None),
        Order.is_deleted == False,
        Order.order_status.not_in([OrderStatus.cancelled]),
        OrderRoom.check_in_date < check_out,
        OrderRoom.check_out_date > check_in,
    ]
    if exclude_order_id:
        order_conflict_conds.append(Order.order_id != exclude_order_id)

    conflicted_orders = await db.execute(
        select(OrderRoom.room_id)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .distinct()
        .where(*order_conflict_conds)
    )
    conflicted = {r[0] for r in conflicted_orders.all()}

    conflicted_blocks = await db.execute(
        select(RoomBlock.room_id)
        .distinct()
        .where(
            RoomBlock.start_date < check_out,
            RoomBlock.end_date > check_in,
        )
    )
    blocked = {r[0] for r in conflicted_blocks.all()}

    from app.core.datetime_helpers import today_cn
    from app.services.room_presence import current_room_presence
    today = today_cn()
    if check_in <= today < check_out:
        present = await current_room_presence(db, today, exclude_order_id=exclude_order_id)
        blocked.update(room.room_id for room in present if room.checkout_date < today)

    return sorted(all_room_ids - conflicted - blocked)


async def compute_effective_status(db: AsyncSession, on_date: date) -> dict[str, str]:
    """返回 {room_id: effective_status}，按 on_date 从订单实时推算房态。

    优先级（高→低）：
      1. 物理/事件态 room_status ∈ {维修, 锁房, 待清扫, 清扫中} —— 手动/事件驱动，
         订单算不出，优先级最高（业主/前台决策）。
      2. 在住(occupied) —— 今天按实际逐房入住/退房证据，与首页同源。
         预计退房日到来不代表客人已走；历史/未来仍使用预订日期区间投影。
      3. 已预订(reserved) —— on_date「今明两天内」(on_date..on_date+1) 有未取消订单到店，
         且该房当日不在住。更远的未来预订仍算空置。
      4. 锁房区间(RoomBlock) —— 覆盖当日 → 维修/锁房。
      5. 空置(available)。
    """
    from app.models.room import Room, RoomStatus

    rooms = (await db.execute(select(Room.room_id, Room.room_status).where(Room.is_deleted == False))).all()

    from app.core.datetime_helpers import today_cn
    from app.services.room_presence import current_room_presence, room_arrival_predicate
    if on_date == today_cn():
        occupied = {room.room_id for room in await current_room_presence(db, on_date)}
    else:
        # Retain date-bounded projections: an overdue actual stay today must
        # not silently block every future date or rewrite historical calendars.
        occ = await db.execute(
            select(OrderRoom.room_id).distinct().join(Order, Order.order_id == OrderRoom.order_id)
            .where(Order.is_deleted == False,
                   Order.order_status.in_([OrderStatus.checked_in, OrderStatus.pending_checkout]),
                   OrderRoom.room_id.isnot(None), OrderRoom.checked_out_at.is_(None),
                   room_arrival_predicate(),
                   OrderRoom.check_in_date <= on_date, OrderRoom.check_out_date > on_date)
        )
        occupied = {r[0] for r in occ.all()}

    # 已预订：今明两天内到店的未取消/未完成订单（尚未入住）
    INACTIVE = [OrderStatus.cancelled, OrderStatus.completed]
    res = await db.execute(
        select(OrderRoom.room_id).distinct().join(Order, Order.order_id == OrderRoom.order_id)
        .where(Order.is_deleted == False, Order.order_status.not_in(INACTIVE),
               OrderRoom.room_id.isnot(None),
               OrderRoom.check_in_date >= on_date,
               OrderRoom.check_in_date <= on_date + timedelta(days=1),
               OrderRoom.check_out_date > on_date)
    )
    reserved = {r[0] for r in res.all()}

    blk = await db.execute(
        select(RoomBlock.room_id, RoomBlock.block_type)
        .where(RoomBlock.start_date <= on_date, RoomBlock.end_date > on_date)
    )
    block_type = {r[0]: r[1] for r in blk.all()}

    KEEP = {RoomStatus.maintenance, RoomStatus.locked,
            RoomStatus.pending_clean, RoomStatus.cleaning}
    out: dict[str, str] = {}
    for rid, st in rooms:
        if st in KEEP:
            out[rid] = st.value
        elif rid in occupied:
            out[rid] = RoomStatus.occupied.value
        elif rid in reserved:
            out[rid] = RoomStatus.reserved.value
        elif rid in block_type:
            bt = block_type[rid]
            bt_val = bt.value if hasattr(bt, "value") else bt
            out[rid] = "maintenance" if bt_val == "maintenance" else "locked"
        else:
            out[rid] = RoomStatus.available.value
    return out
