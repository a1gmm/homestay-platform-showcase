"""操作追溯（杀手锏）—— 定位订单 + 拉完整 audit 时间线 + OTA 盲区标记。

服务端一次完成「定位候选单 + 每单完整操作时间线」，保住单发意图分类、无 agentic 循环。
承重事实（谁/几点/改了啥/人还是系统）全在 timeline.py 里确定性算好，交给 narrate 只写一句总结。
"""
from __future__ import annotations

from datetime import date

from sqlalchemy import or_, select

from app.models.audit_log import AuditLog
from app.models.order import OTA_PLATFORM_CHANNELS, Order
from app.models.order_room import OrderRoom
from app.models.user import User
from app.services.assistant.timeline import build_events, render_timeline

# 候选单超过这个数就不逐单拉时间线，返回候选让上层回显「你说的是哪一单」（铁律：不瞎猜）。
_MAX_DETAIL = 5


def _ota_blind_spot(order: Order) -> bool:
    """OTA 平台单：同步侧（携程/bypms）改动不写本库审计 → 盲区（坑③）。"""
    return order.channel in OTA_PLATFORM_CHANNELS


async def _find_orders(db, guest_name: str | None, day: date | None, room: str | None) -> list[Order]:
    stmt = select(Order)
    conds = []
    if guest_name:
        conds.append(Order.guest_name.ilike(f"%{guest_name}%"))
    if day:
        # 住期覆盖该日（含端点）
        conds.append((Order.check_in_date <= day) & (Order.check_out_date >= day))
    if room:
        room_order_ids = select(OrderRoom.order_id).where(OrderRoom.room_id == room)
        conds.append(or_(Order.room_id == room, Order.order_id.in_(room_order_ids)))
    for c in conds:
        stmt = stmt.where(c)
    stmt = stmt.order_by(Order.check_in_date.desc()).limit(20)
    return list((await db.execute(stmt)).scalars().all())


async def _timeline_for_order(db, order_id: str) -> dict:
    rows = list((await db.execute(
        select(AuditLog)
        .where(AuditLog.resource_type == "order", AuditLog.resource_id == order_id)
        .order_by(AuditLog.created_at.asc())
    )).scalars().all())

    op_ids = {r.operator_id for r in rows if r.operator_id and r.operator_id != "system"}
    name_map: dict[str, str] = {}
    if op_ids:
        users = (await db.execute(select(User).where(User.user_id.in_(op_ids)))).scalars().all()
        name_map = {u.user_id: u.display_name for u in users}

    events = build_events(rows, name_map)
    return render_timeline(events)


def _brief(order: Order) -> dict:
    return {
        "order_id": order.order_id,
        "guest_name": order.guest_name,
        "channel": order.channel.value if order.channel else None,
        "room_id": order.room_id,
        "check_in_date": str(order.check_in_date) if order.check_in_date else None,
        "check_out_date": str(order.check_out_date) if order.check_out_date else None,
        "status": order.order_status.value if order.order_status else None,
        "ota_blind_spot": _ota_blind_spot(order),
    }


async def investigate_order(
    db,
    guest_name: str | None = None,
    on_date: date | None = None,
    room: str | None = None,
) -> dict:
    """定位订单并拉取追溯时间线。

    - 0 单：not_found。
    - >_MAX_DETAIL 单：ambiguous，只返回候选简讯，交上层回显让用户确认是哪一单。
    - 1.._MAX_DETAIL 单：每单附完整确定性时间线 + OTA 盲区标记。
    """
    orders = await _find_orders(db, guest_name, on_date, room)

    if not orders:
        return {"matched": 0, "found": False, "ambiguous": False, "orders": [], "candidates": []}

    if len(orders) > _MAX_DETAIL:
        return {
            "matched": len(orders),
            "found": True,
            "ambiguous": True,
            "orders": [],
            "candidates": [_brief(o) for o in orders],
        }

    detailed = []
    for o in orders:
        tl = await _timeline_for_order(db, o.order_id)
        detailed.append({
            **_brief(o),
            "timeline_text": tl["text"],
            "timeline_rows": tl["rows"],
        })
    return {
        "matched": len(orders),
        "found": True,
        "ambiguous": False,
        "orders": detailed,
        "candidates": [],
    }
