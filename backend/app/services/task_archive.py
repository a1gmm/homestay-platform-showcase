"""Administrative retirement of old obligations, never cleaning completion."""
from datetime import datetime, timedelta, timezone
import hashlib
import json

from fastapi import HTTPException
from sqlalchemy import and_, select

from app.models.order import Order, OrderStatus
from app.models.task import Task, TaskStatus, TaskType
from app.services.audit import log_action_tx
from app.services.task_lifecycle import ACTIVE_STATUSES


def historical_task_condition(now):
    # Age and a terminal order make a task a review candidate, not proof of work.
    return and_(Task.status.in_(ACTIVE_STATUSES), Task.task_type == TaskType.cleaning,
                Task.created_at <= now - timedelta(days=7),
                select(Order.order_id).where(Order.order_id == Task.order_id,
                    Order.order_status.in_((OrderStatus.completed, OrderStatus.cancelled))).exists())


async def archive_historical_task(db, task_id, *, apply=False, reason="", expected_fingerprint=None,
                                  operator_id=None):
    task = (await db.execute(select(Task).where(Task.task_id == task_id)
                            .with_for_update().execution_options(populate_existing=True))).scalar_one_or_none()
    if task is None:
        raise HTTPException(404, "任务不存在")
    eligible = (await db.execute(select(Task.task_id).where(
        Task.task_id == task_id, historical_task_condition(datetime.now(timezone.utc))
    ))).scalar_one_or_none()
    if not eligible:
        raise HTTPException(409, "仅可归档创建满 7 天且关联订单已完成或取消的未完成保洁任务；请先核实订单和当前任务")
    order = (await db.execute(select(Order).where(Order.order_id == task.order_id)
                             .execution_options(populate_existing=True))).scalar_one()
    snapshot = {c.name: getattr(task, c.name) for c in Task.__table__.columns}
    # Include all task fields so assignment, notes, submission, etc. invalidate preview.
    fingerprint = hashlib.sha256(json.dumps(
        {"task": snapshot, "order_status": order.order_status.value},
        sort_keys=True, default=str, ensure_ascii=False).encode()).hexdigest()
    result = {"task_id": task.task_id, "title": task.title, "room_id": task.room_id,
              "order_id": task.order_id, "order_status": order.order_status.value,
              "fingerprint": fingerprint, "applied": False,
              "effect": "仅将这项历史任务标为已取消，保留原记录；不确认保洁完成，不修改房态、订单、费用或门锁。"}
    if apply:
        if not reason.strip():
            raise HTTPException(422, "请填写已核实的归档原因；仍需执行的保洁请保留并安排负责人")
        if expected_fingerprint != fingerprint:
            raise HTTPException(409, "任务或订单已变化，请重新预览后核实")
        before = {"status": task.status.value, "review_status": task.review_status}
        task.status = TaskStatus.cancelled
        # Preserve submission/review evidence; terminal status prevents review/restart.
        await log_action_tx(db, operator_id, "task.archive", "task", task.task_id,
                            before_data=before, after_data={"status": "cancelled", "fingerprint": fingerprint,
                            "order_id": order.order_id, "order_status": order.order_status.value},
                            notes=reason.strip())
        result["applied"] = True
    return result
