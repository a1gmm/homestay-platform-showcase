"""Read-only, month-scoped display locators. Never part of confirmation hashes."""
from datetime import date
from sqlalchemy import select
from app.models.monthly_close import MONTHLY_CLOSE_STEP_KEYS


async def issue_order_locators(db, billing_month: str, evidences) -> dict[str, str]:
    """Batch human-readable locators, restricted to this month's checkout legs."""
    from app.models.order import Order
    from app.models.order_room import OrderRoom
    from app.models.room import Room

    order_ids = {issue["order_id"] for evidence in evidences
                 if evidence.step_key in MONTHLY_CLOSE_STEP_KEYS
                 for issue in evidence.snapshot.get("issues", []) if issue.get("order_id")}
    if not order_ids:
        return {}
    year, month = map(int, billing_month.split("-"))
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    legs = select(Order.order_id, Order.guest_name, Room.room_name,
                  OrderRoom.check_in_date, OrderRoom.check_out_date).join(
        OrderRoom, OrderRoom.order_id == Order.order_id).outerjoin(
        Room, Room.room_id == OrderRoom.room_id).where(
        Order.order_id.in_(order_ids), Order.is_deleted.is_(False),
        OrderRoom.check_out_date >= start, OrderRoom.check_out_date < end)
    # Header dates are only a compatibility fallback for orders without legs.
    # An out-of-month leg must never become visible through its order header.
    legacy = select(Order.order_id, Order.guest_name, Room.room_name,
                    Order.check_in_date, Order.check_out_date).outerjoin(
        Room, Room.room_id == Order.room_id).where(
        Order.order_id.in_(order_ids), Order.is_deleted.is_(False),
        ~Order.rooms.any(), Order.check_out_date >= start, Order.check_out_date < end)
    current_rows = legs.union_all(legacy).subquery()
    rows = (await db.execute(select(current_rows).order_by(
        current_rows.c.order_id, current_rows.c.check_in_date,
        current_rows.c.check_out_date, current_rows.c.room_name))).all()
    locators: dict[str, list[str]] = {}
    names: dict[str, str] = {}
    for order_id, guest_name, room_name, check_in, check_out in rows:
        names[order_id] = guest_name or "未登记姓名的客人"
        locators.setdefault(order_id, []).append(
            f"{room_name or '待排房'}（入住 {check_in.isoformat()}，退房 {check_out.isoformat()}）")
    return {order_id: f"{names[order_id]} · {'、'.join(rooms)}"
            for order_id, rooms in locators.items()}

