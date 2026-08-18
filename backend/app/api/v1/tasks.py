from fastapi import APIRouter, HTTPException, Query
from sqlalchemy import select, and_, or_
from typing import Optional
from pydantic import BaseModel
from datetime import datetime, timezone
import uuid

from app.core.deps import DBSession, CurrentUser
from app.models.task import Task, TaskStatus, TaskType, TaskPriority, ReviewStatus
from app.models.order import Order, OrderStatus, CleaningStatus
from app.models.room import Room, RoomStatus
from app.services.audit import log_action_tx

router = APIRouter(prefix="/tasks", tags=["tasks"])


class TaskCreate(BaseModel):
    task_type: TaskType = TaskType.custom
    title: str
    description: Optional[str] = None
    order_id: Optional[str] = None
    room_id: Optional[str] = None
    assignee_id: Optional[str] = None
    priority: TaskPriority = TaskPriority.medium
    deadline: Optional[datetime] = None
    notes: Optional[str] = None


class TaskUpdate(BaseModel):
    title: Optional[str] = None
    description: Optional[str] = None
    assignee_id: Optional[str] = None
    status: Optional[TaskStatus] = None
    priority: Optional[TaskPriority] = None
    deadline: Optional[datetime] = None
    notes: Optional[str] = None


class TaskSubmitRequest(BaseModel):
    notes: Optional[str] = None


class TaskReviewRequest(BaseModel):
    approved: bool
    rejection_reason: Optional[str] = None


class TaskOut(BaseModel):
    task_id: str
    task_type: TaskType
    title: str
    description: Optional[str]
    order_id: Optional[str]
    room_id: Optional[str]
    assignee_id: Optional[str]
    status: TaskStatus
    priority: TaskPriority
    deadline: Optional[datetime]
    completed_at: Optional[datetime]
    notes: Optional[str]
    submitted_at: Optional[datetime] = None
    review_status: Optional[str] = None
    reviewer_id: Optional[str] = None
    reviewed_at: Optional[datetime] = None
    rejection_reason: Optional[str] = None
    created_at: datetime
    model_config = {"from_attributes": True}


@router.get("", response_model=list[TaskOut])
async def list_tasks(
    db: DBSession,
    current_user: CurrentUser,
    status: Optional[TaskStatus] = Query(default=None),
    order_id: Optional[str] = Query(default=None),
    assignee_id: Optional[str] = Query(default=None),
    overdue_only: bool = Query(default=False),
):
    q = select(Task)

    # Cleaner 看自己的任务 + 所有未派单的清扫任务（可抢单）。
    # 非清扫且未派单的任务不属于保洁职责范围，不显示。
    if current_user["role"] == "cleaner":
        q = q.where(
            or_(
                Task.assignee_id == current_user["user_id"],
                and_(Task.assignee_id.is_(None), Task.task_type == TaskType.cleaning),
            )
        )
    elif assignee_id:
        q = q.where(Task.assignee_id == assignee_id)

    if status:
        q = q.where(Task.status == status)
    if order_id:
        q = q.where(Task.order_id == order_id)
    if overdue_only:
        q = q.where(Task.deadline < datetime.now(timezone.utc), Task.status != TaskStatus.done)

    result = await db.execute(q.order_by(Task.deadline.asc().nullslast(), Task.created_at.desc()))
    return result.scalars().all()


@router.post("", response_model=TaskOut, status_code=201)
async def create_task(body: TaskCreate, db: DBSession, current_user: CurrentUser):
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="无权创建任务")
    task = Task(
        task_id="TSK-" + uuid.uuid4().hex[:12].upper(),
        created_by=current_user["user_id"],
        **body.model_dump(),
    )
    db.add(task)
    await db.commit()
    await db.refresh(task)
    return task


@router.patch("/{task_id}", response_model=TaskOut)
async def update_task(task_id: str, body: TaskUpdate, db: DBSession, current_user: CurrentUser):
    result = await db.execute(select(Task).where(Task.task_id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # Cleaner 可操作自己的任务；未派单的清扫任务在操作时自动 claim 给当前保洁。
    auto_claimed = False
    if current_user["role"] == "cleaner":
        if task.assignee_id is None and task.task_type == TaskType.cleaning:
            task.assignee_id = current_user["user_id"]
            auto_claimed = True
        elif task.assignee_id != current_user["user_id"]:
            raise HTTPException(status_code=403, detail="只能操作分配给自己的任务")

    # 清扫任务的"完成"必须走 /tasks/{id}/review,触发"房态恢复+订单完成"联动。
    # 直接 PATCH status=done 跳过 review_task → 房间没回 available + 订单没 completed,
    # 前台看似完成实际状态不一致(2026-05-29 孙鹏飞反馈的 root cause)。
    if (
        body.status == TaskStatus.done
        and task.task_type == TaskType.cleaning
        and task.review_status != ReviewStatus.approved.value
    ):
        raise HTTPException(
            status_code=400,
            detail="清扫任务需要通过查房审核才能完成,请在管家端使用'查房通过'",
        )

    for field, value in body.model_dump(exclude_none=True).items():
        setattr(task, field, value)

    if body.status == TaskStatus.done and not task.completed_at:
        task.completed_at = datetime.now(timezone.utc)

    # 房态联动:保洁认领并开始打扫(cleaning task → in_progress) → room.status = cleaning
    if (
        body.status == TaskStatus.in_progress
        and task.task_type == TaskType.cleaning
        and task.room_id
    ):
        room = (await db.execute(select(Room).where(Room.room_id == task.room_id))).scalar_one_or_none()
        if room and room.room_status in (RoomStatus.pending_clean, RoomStatus.occupied):
            room.room_status = RoomStatus.cleaning

    await log_action_tx(db, current_user["user_id"], "task.update", "task", task_id)
    if auto_claimed:
        await log_action_tx(db, current_user["user_id"], "task.auto_claim", "task", task_id,
                            after_data={"assignee_id": current_user["user_id"]})
    await db.commit()
    await db.refresh(task)
    return task


@router.delete("/{task_id}")
async def delete_task(task_id: str, db: DBSession, current_user: CurrentUser):
    """删除运营任务。管理员和运营可以删除任意任务，保洁无权删除。"""
    if current_user["role"] not in ("admin", "operator"):
        raise HTTPException(status_code=403, detail="无权删除任务")

    result = await db.execute(select(Task).where(Task.task_id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    await db.delete(task)
    await log_action_tx(db, current_user["user_id"], "task.delete", "task", task_id)
    await db.commit()
    return {"message": "任务已删除"}


# ─── Feature 4: Cleaning review workflow ──────────────────────────────────────

@router.post("/{task_id}/submit", response_model=TaskOut)
async def submit_task(task_id: str, body: TaskSubmitRequest, db: DBSession, current_user: CurrentUser):
    """保洁提交完工，等待审核。"""
    result = await db.execute(select(Task).where(Task.task_id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    # 同 update：保洁可以认领未派单的清扫任务，并在 /submit 时一并 claim。
    auto_claimed = False
    if current_user["role"] == "cleaner":
        if task.assignee_id is None and task.task_type == TaskType.cleaning:
            task.assignee_id = current_user["user_id"]
            auto_claimed = True
        elif task.assignee_id != current_user["user_id"]:
            raise HTTPException(status_code=403, detail="只能提交分配给自己的任务")

    if task.status == TaskStatus.done:
        raise HTTPException(status_code=400, detail="任务已完成，无法重复提交")

    task.submitted_at = datetime.now(timezone.utc)
    task.review_status = ReviewStatus.pending_review.value
    task.status = TaskStatus.in_progress
    if body.notes:
        task.notes = body.notes

    await log_action_tx(db, current_user["user_id"], "task.submit", "task", task_id)
    if auto_claimed:
        await log_action_tx(db, current_user["user_id"], "task.auto_claim", "task", task_id,
                            after_data={"assignee_id": current_user["user_id"]})
    await db.commit()
    await db.refresh(task)
    return task


@router.post("/{task_id}/review", response_model=TaskOut)
async def review_task(task_id: str, body: TaskReviewRequest, db: DBSession, current_user: CurrentUser):
    """管理员/运营/管家审核保洁任务（即"查房"动作）。
    清扫任务通过后：room.status=available + cleaning_status=inspected。
    订单完成不在此处触发（收齐房费的唯一收口在 /transition，见 #40）。
    """
    if current_user["role"] not in ("admin", "operator", "keeper"):
        raise HTTPException(status_code=403, detail="无权审核任务")

    result = await db.execute(select(Task).where(Task.task_id == task_id))
    task = result.scalar_one_or_none()
    if not task:
        raise HTTPException(status_code=404, detail="任务不存在")

    if task.review_status != ReviewStatus.pending_review.value:
        raise HTTPException(status_code=400, detail="该任务不在待审核状态")

    task.reviewer_id = current_user["user_id"]
    task.reviewed_at = datetime.now(timezone.utc)

    if body.approved:
        task.review_status = ReviewStatus.approved.value
        task.status = TaskStatus.done
        task.completed_at = datetime.now(timezone.utc)

        # 查房通过 → 恢复房间可入住 + 标记清扫已验收（仅清扫类任务触发）。
        # 注意:不在此处完成订单。订单完成的「收齐房费」唯一收口在 /transition,
        # 保洁通常早于平台到账,在此强制 completed 会系统性关闭未收款订单 (#40)。
        if task.task_type == TaskType.cleaning:
            if task.room_id:
                room_res = await db.execute(select(Room).where(Room.room_id == task.room_id))
                room = room_res.scalar_one_or_none()
                # 当前流程退房后房间处于 pending_clean/cleaning;一并纳入恢复白名单,
                # 否则验收后房间永久卡在 cleaning。
                if room and room.room_status in (
                    RoomStatus.occupied, RoomStatus.reserved,
                    RoomStatus.pending_clean, RoomStatus.cleaning,
                ):
                    room.room_status = RoomStatus.available
                # 退房打扫经「查房通过」完成 → 同样向房东计一笔保洁费(65)。
                # 与飞书「打扫完了」互斥（review_status 门 + 原子 done 判定），不重复扣。
                if room:
                    from app.services import service_fee_ledger
                    from app.services.cleaning import add_checkout_cleaning_charge
                    try:
                        await add_checkout_cleaning_charge(
                            db, room=room, order_id=task.order_id
                        )
                    except service_fee_ledger.CheckoutServiceFeeWrongMonthError as exc:
                        await db.rollback()
                        raise HTTPException(status_code=409, detail=exc.to_detail()) from exc
            if task.order_id:
                order_res = await db.execute(select(Order).where(Order.order_id == task.order_id))
                order = order_res.scalar_one_or_none()
                if order and order.order_status not in (OrderStatus.cancelled, OrderStatus.completed):
                    order.cleaning_status = CleaningStatus.inspected
    else:
        task.review_status = ReviewStatus.rejected.value
        task.rejection_reason = body.rejection_reason
        task.status = TaskStatus.pending  # Reset to pending for redo

    await log_action_tx(db, current_user["user_id"], "task.review", "task", task_id,
                        after_data={"approved": body.approved})
    await db.commit()
    # 保洁查房通过 → 撤该房保洁码（与飞书「打扫完了」同效，方案 B）。fail-safe，
    # 门锁问题不影响查房回写；兜底还有次日12:00过期。
    if body.approved and task.task_type == TaskType.cleaning and task.room_id:
        from app.services.lock.hooks import revoke_cleaning_code_on_done
        await revoke_cleaning_code_on_done(db, task.room_id, order_id=task.order_id)
    await db.refresh(task)
    # 查房通过 = 网页侧完工终态 → 改灰保洁群卡（best-effort，与飞书按钮路径共用钩子）。
    # 天然幂等：本函数开头已强制 review_status == pending_review 才能进入，approved
    # 后 review_status 变为 approved，同一任务不可能再次走到这里，无需额外重复守卫。
    if body.approved:
        from app.services.cleaning import grey_cleaning_card_best_effort
        await grey_cleaning_card_best_effort(db, task)
    return task
