"""Per-room arrival evidence, with compatibility for pre-marker stays."""
from dataclasses import dataclass
from datetime import date

from sqlalchemy import and_, or_, select
from sqlalchemy.orm import aliased

from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.room import Room


def checked_in_rooms(rooms):
    # The original migration deliberately did not backfill old stays. Only
    # fall back to whole-order checkin when no room has a marker at all.
    has_markers = any(room.checked_in_at is not None for room in rooms)
    return [room for room in rooms
            if room.room_id and room.checked_out_at is None
            and (room.checked_in_at is not None or not has_markers)]


def room_arrival_predicate():
    sibling = aliased(OrderRoom)
    has_markers = select(sibling.order_room_id).where(
        sibling.order_id == OrderRoom.order_id,
        sibling.checked_in_at.isnot(None),
    ).correlate(OrderRoom).exists()
    return or_(OrderRoom.checked_in_at.isnot(None), ~has_markers)


def current_room_presence_predicate(on_date: date):
    """Actual presence is not ended by a planned checkout date.

    pending_checkout means 已退房待收款, not a guest awaiting departure.
    Legacy orders without arrival markers only infer arrival once their stay
    has started; explicit early arrival remains evidence even before that date.
    """
    return and_(
        Order.is_deleted.is_(False),
        Order.order_status == OrderStatus.checked_in,
        OrderRoom.room_id.isnot(None),
        OrderRoom.checked_out_at.is_(None),
        room_arrival_predicate(),
        or_(OrderRoom.checked_in_at.isnot(None), OrderRoom.check_in_date <= on_date),
    )


@dataclass(frozen=True)
class CurrentRoomPresence:
    room_id: str
    room_name: str
    checkout_date: date
    order_id: str | None = None


async def current_room_presence(
    db, on_date: date, *, room_id: str | None = None,
    exclude_order_id: str | None = None,
    exclude_order_room_id: str | None = None,
    exclude_stay_group_id: str | None = None,
) -> list[CurrentRoomPresence]:
    """Resolve current rooms and the planned end of each physical room leg.

    Same-room continuations may leave the anchor checked_in while their tail
    has already checked out. Resolve their per-room evidence in one batch so
    old anchors cannot resurrect a departed guest. A gap or a room change
    starts a new physical leg, including managed splits that later return to
    a previous room. This read never changes order state or arrival markers.
    """
    statement = (
        select(OrderRoom, Order, Room.room_name)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .join(Room, Room.room_id == OrderRoom.room_id)
        .where(Room.is_deleted.is_(False), current_room_presence_predicate(on_date))
    )
    if room_id:
        statement = statement.where(OrderRoom.room_id == room_id)
    if exclude_stay_group_id:
        statement = statement.where(or_(Order.stay_group_id.is_(None),
                                        Order.stay_group_id != exclude_stay_group_id))
    candidates = (await db.execute(statement)).all()
    group_ids = {order.stay_group_id for _, order, _ in candidates if order.stay_group_id}
    by_group_room: dict[tuple[str, str], list] = {}
    if group_ids:
        members = (await db.execute(
            select(OrderRoom, Order.order_status, Order.stay_group_id)
            .join(Order, Order.order_id == OrderRoom.order_id)
            .where(Order.stay_group_id.in_(group_ids), Order.is_deleted.is_(False),
                   Order.order_status != OrderStatus.cancelled,
                   OrderRoom.room_id.isnot(None))
            .order_by(OrderRoom.check_in_date, OrderRoom.check_out_date, Order.order_id)
        )).all()
        for room, status, group_id in members:
            by_group_room.setdefault((group_id, room.room_id), []).append((room, status))

    departed = {OrderStatus.pending_checkout, OrderStatus.pending_payment, OrderStatus.completed}
    leg_by_room_row: dict[str, list] = {}
    for members in by_group_room.values():
        leg = []
        leg_end = None
        for member, status in members:
            if leg_end is None or member.check_in_date > leg_end:
                leg = []
                leg_end = member.check_out_date
            else:
                leg_end = max(leg_end, member.check_out_date)
            leg.append((member, status))
            leg_by_room_row[member.order_room_id] = leg

    present: dict[str, CurrentRoomPresence] = {}
    for room, order, name in candidates:
        leg = leg_by_room_row.get(room.order_room_id, [(room, order.order_status)])
        checkout_date = max(member.check_out_date for member, _ in leg)
        leg_order_ids = {member.order_id for member, _ in leg}
        leg_room_ids = {member.order_room_id for member, _ in leg}
        closed = False
        leg_started = False
        for member, status in leg:
            if member.order_room_id == room.order_room_id:
                leg_started = True
                continue
            if not leg_started:
                continue
            if member.checked_out_at is not None or status in departed:
                closed = True
                break
        # A continuation tail can own the operation while its old anchor
        # supplies the arrival evidence. Exclude that same physical leg,
        # without suppressing another guest's independent stay in this room.
        if (closed or exclude_order_id in leg_order_ids
                or exclude_order_room_id in leg_room_ids):
            continue
        previous = present.get(room.room_id)
        if previous is None or checkout_date > previous.checkout_date:
            present[room.room_id] = CurrentRoomPresence(room.room_id, name, checkout_date, order.order_id)
    return sorted(present.values(), key=lambda item: (item.room_name, item.room_id))
