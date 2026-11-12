"""Resolve a new order's continuation decision before committing any business data.

Names/phones identify candidates, never authorize linking. The operator's choice
is bound to the form and current candidates and is revalidated under room locks.
"""
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.exc import DBAPIError

from app.models.order import Order
from app.models.order_room import OrderRoom
from app.models.room import Room
from app.services.checkout_continuation import _LINKABLE_STATUSES


async def prepare_creation_continuation(db, body):
    room_ids = sorted({r.room_id for r in body.rooms if r.room_id})
    # All creators use the same room lock as the overlap guard. Sorting also
    # prevents opposite-order multi-room requests from deadlocking each other.
    try:
        available_ids = set((await db.execute(select(Room.room_id).where(
            Room.room_id.in_(room_ids), Room.is_deleted.is_(False))
            .order_by(Room.room_id).with_for_update(nowait=True))).scalars().all())
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise HTTPException(409, "房态正在更新，本次尚未保存，请稍后再点保存。") from exc
        raise
    if available_ids != set(room_ids):
        raise HTTPException(409, "所选房间不存在或已下线，请重新选择房间。")
    identity = [func.trim(Order.guest_name) == body.guest_name.strip()]
    if body.guest_phone:
        identity.append(Order.guest_phone == body.guest_phone)
    matches = {}
    for r in body.rooms:
        if not r.room_id:
            continue
        try:
            rows = (await db.execute(select(Order, OrderRoom)
                .join(OrderRoom, OrderRoom.order_id == Order.order_id)
                .where(Order.is_deleted.is_(False), Order.order_status.in_(_LINKABLE_STATUSES),
                       OrderRoom.room_id == r.room_id, or_(*identity),
                       or_(OrderRoom.check_out_date == r.check_in_date,
                           OrderRoom.check_in_date == r.check_out_date))
                .order_by(Order.order_id, OrderRoom.order_room_id)
                .with_for_update(of=Order, nowait=True))).all()
        except DBAPIError as exc:
            # Editors can already hold Order before requesting Room. Never wait
            # for that Order while holding Room: release via request rollback.
            if getattr(exc.orig, "sqlstate", None) == "55P03":
                raise HTTPException(409, "原订单正在被修改，本次尚未保存，请稍后再点保存。") from exc
            raise
        for order, room in rows:
            key = order.stay_group_id or order.order_id
            candidate = matches.setdefault(key, {"order": order, "rows": []})
            candidate["rows"].append({"order_id": order.order_id, "room_id": room.room_id,
                "check_in_date": str(room.check_in_date), "check_out_date": str(room.check_out_date),
                "direction": "previous" if room.check_out_date == r.check_in_date else "next",
                "status": order.order_status.value, "updated_at": str(order.updated_at)})
            # Prefer a preceding segment when filling a gap in an existing group.
            if room.check_out_date == r.check_in_date:
                candidate["order"] = order
    candidates = [{"order_id": m["order"].order_id, "stay_group_id": m["order"].stay_group_id,
                   "guest_name": m["order"].guest_name,
                   "rooms": m["rows"]} for _, m in sorted(matches.items())]
    fingerprint = hashlib.sha256(json.dumps({
        "guest_name": body.guest_name, "guest_phone": body.guest_phone,
        "rooms": [r.model_dump(mode="json") for r in body.rooms],
        "candidates": candidates,
    }, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    choice = body.continuation
    if not candidates and choice is None:
        return None
    if choice is None or choice.fingerprint != fingerprint or not candidates:
        raise HTTPException(409, detail={"code": "continuation_decision_required",
            "message": "发现同房、日期相接的订单，请确认是否为同一位客人续住。确认后将同时创建并关联，无需另行操作。",
            "fingerprint": fingerprint, "candidates": candidates})
    if choice.action == "separate":
        return {"action": "separate", "fingerprint": fingerprint,
                "candidate_order_ids": [c["order_id"] for c in candidates]}
    selected = next((m for m in matches.values() if m["order"].order_id == choice.order_id), None)
    if selected is None:
        raise HTTPException(409, "续住候选已变化，请重新提交并核对原订单。")
    from app.services.stay_group import is_managed_split
    if await is_managed_split(db, selected["order"].stay_group_id):
        raise HTTPException(409, "该住宿有专用分段结算，请从原订单的住宿段更正入口处理续住。")
    # A single new order cannot silently combine two independent stays.
    if len(matches) > 1:
        raise HTTPException(409, "这些日期连接了不同住宿，请按房间和实际客人分别录入，不能把不同住宿合成一组。")
    return {"action": "link", "order": selected["order"],
            "direction": next(row["direction"] for row in selected["rows"]
                              if row["order_id"] == selected["order"].order_id),
            "fingerprint": fingerprint}


async def apply_creation_continuation(db, order, prepared, operator_id):
    if not prepared:
        return
    from app.services.audit import log_action_tx
    from app.services.manual_override import apply_snapshot_manual_locks
    from app.services.stay_group import link_continuation
    if prepared["action"] == "separate":
        order.metadata_ = {**(order.metadata_ or {}), "creation_continuation_decision": prepared}
        return
    other = prepared["order"]
    base, nxt = (other, order) if prepared["direction"] == "previous" else (order, other)
    before = {o.order_id: o.stay_group_id for o in (base, nxt)}
    try:
        gid = await link_continuation(db, base, nxt)
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    for o in (base, nxt):
        apply_snapshot_manual_locks(o, {"stay_group_id": before[o.order_id]},
                                    {"stay_group_id": gid}, source="human")
    from app.models.order import OrderStatus
    from app.services.continuation_fees import continuation_fee_context, consolidate_continuation_fees
    from app.services.service_fee_ledger import CheckoutExpenseRecognitionError
    room_ids = (await db.execute(select(OrderRoom.room_id).where(
        OrderRoom.order_id == order.order_id, OrderRoom.room_id.isnot(None)))).scalars().all()
    try:
        for room_id in sorted(set(room_ids)):
            room = await db.get(Room, room_id)
            context = await continuation_fee_context(db, order, room, fail_fast=True)
            if context and context["final"].order_status in (
                OrderStatus.completed, OrderStatus.pending_checkout, OrderStatus.pending_payment,
            ):
                await consolidate_continuation_fees(db, room, context, operator_id)
            if context:
                from app.models.task import Task, TaskStatus, TaskType
                intermediate_ids = [o.order_id for o, _ in context["rows"][:-1]]
                tasks = (await db.execute(select(Task).where(
                    Task.order_id.in_(intermediate_ids), Task.room_id == room_id,
                    Task.task_type == TaskType.cleaning, Task.status == TaskStatus.pending,
                    Task.cleaning_started_at.is_(None),
                    Task.submitted_at.is_(None), Task.completed_at.is_(None),
                ).with_for_update(nowait=True))).scalars().all()
                for task in tasks:
                    task.status = TaskStatus.cancelled
                    await log_action_tx(db, operator_id, "task.cancel", "task", task.task_id,
                        notes="建单确认续住，取消中间段尚未开始的退房清扫任务")
    except CheckoutExpenseRecognitionError as exc:
        raise HTTPException(409, detail=exc.to_detail()) from exc
    except DBAPIError as exc:
        if getattr(exc.orig, "sqlstate", None) == "55P03":
            raise HTTPException(409, "相关退房、费用或清扫资料正在处理，本次尚未保存，请稍后再点保存。") from exc
        raise
    await log_action_tx(db, operator_id, "order.link_continuation", "order", order.order_id,
        before_data={"groups": before}, after_data={"stay_group_id": gid,
            "related_order_id": other.order_id, "decision_fingerprint": prepared["fingerprint"]},
        notes="建单时确认同一位客人续住，创建和关联同一事务保存")


async def extend_created_continuation_codes(db, order):
    """After commit, refresh existing live guest codes only; never issue historical codes."""
    if not order.stay_group_id:
        return
    from app.models.order import OrderStatus
    from app.core.datetime_helpers import now_cn
    from app.services import stay_group
    active = await db.scalar(select(Order.order_id).where(
        Order.stay_group_id == order.stay_group_id, Order.is_deleted.is_(False),
        Order.order_status == OrderStatus.checked_in,
    ).limit(1))
    if not active:
        return
    final = await stay_group.group_final_checkout_date(db, order.stay_group_id)
    if final is None or final <= now_cn().date():
        return
    try:
        from app.services.lock.factory import get_lock_provider
        from app.services.lock.orchestrator import OrderLockService
        anchor = await stay_group.group_anchor(db, order.stay_group_id)
        if anchor:
            await OrderLockService(get_lock_provider(), db).extend_guest_codes(anchor, final)
    except Exception:
        import logging
        logging.getLogger(__name__).warning("续住建单已保存，门锁延期交后台重试 group=%s", order.stay_group_id)
