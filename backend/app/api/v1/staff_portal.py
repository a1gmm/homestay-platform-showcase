"""
Staff portal 数据接口 —— 管家/保洁在移动端操作订单与任务。

独立于 /orders 管理后台接口,放宽角色:
- keeper / operator / admin 都可调用 handle-checkin / handle-checkout
- 所有 staff role 都可查 cleaners 列表(用于派单)

权限校验在本文件内,不污染现有 /orders API 的 assert_can_write。
"""
from fastapi import APIRouter, BackgroundTasks, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from pydantic import BaseModel
from decimal import Decimal
from typing import Optional
import logging
import uuid

from app.core.datetime_helpers import now_cn
from app.core.deps import DBSession, CurrentUser
from app.core.trial_tags import trial_tag_for_notes
from app.models.user import User, UserRole
from app.models.order import Order, OrderStatus, DepositStatus
from app.models.order_room import OrderRoom
from app.models.room import Room, RoomStatus
from app.models.task import Task, TaskType, TaskStatus, TaskPriority
from app.services.audit import log_action_tx
from app.services.manual_override import apply_snapshot_manual_locks
from app.services.room_availability import check_room_conflict

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/staff", tags=["staff-portal"])


# 允许办理动作的角色
_HANDLE_ROLES = ("admin", "operator", "keeper")


class CleanerBrief(BaseModel):
    user_id: str
    display_name: str
    phone: Optional[str] = None


class HandleCheckinBody(BaseModel):
    notes: Optional[str] = None
    # 管家现场办理入住时收押金（押金 > 0 且未收时生效）。默认 True。
    collect_deposit: bool = True
    # 单间入住（per-room checkin，镜像单间退房）：指定则只入住这一间房；其余未入住房保持待入住。
    # 不传 = 整单入住（把所有已排房、未入住的房一起入住，向后兼容原行为）。押金按整单，
    # 首间入住时收一次（状态守卫保证不重复）。
    order_room_id: Optional[str] = None


class HandleCheckoutBody(BaseModel):
    # 选填（批3 item1「退房暂不派单」）：留空 = 先退房、房置待保洁、建**未分配**清扫任务
    # 进待派池，稍后再派；填了则照常指派给该保洁员。
    cleaner_id: Optional[str] = None
    notes: Optional[str] = None
    # 退押金实退金额（已收押金且 > 0 时生效）。不传 = 全退；少于押金时差额为扣款，须填原因。
    deposit_refund_amount: Optional[Decimal] = None
    deposit_withhold_reason: Optional[str] = None
    # 单间退房（per-room checkout）：指定则只退这一间房；订单还剩在住房时保持 checked_in。
    # 不传 = 整单退房（把所有在住房一起退，向后兼容原行为）。退到最后一间/整单退房时
    # 订单才转 pending_checkout 并结算押金。
    order_room_id: Optional[str] = None


class LockCodeBrief(BaseModel):
    room_id: str
    # active=已下到锁 / pending=锁离线已受理待重试 / failed=需人工 / None=跳过
    status: Optional[str] = None
    skipped_reason: Optional[str] = None


class HandleResult(BaseModel):
    order_id: str
    order_status: str
    created_task_id: Optional[str] = None
    # 门锁客人码下发结果（Phase 1，无 webhook：pending 表示已受理但未确认到锁）。
    lock_codes: Optional[list[LockCodeBrief]] = None


class RevertCheckoutBody(BaseModel):
    reason: str  # 必填，进 audit log


class RevertCheckoutResult(BaseModel):
    order_id: str
    order_status: str
    cancelled_task_ids: list[str] = []
    room_restored: bool = False


class RevertCheckinBody(BaseModel):
    reason: str  # 必填，进 audit log


class RevertCheckinResult(BaseModel):
    order_id: str
    order_status: str
    room_reverted: bool = False
    deposit_reverted: bool = False


@router.get("/cleaners", response_model=list[CleanerBrief])
async def list_cleaners(db: DBSession, current_user: CurrentUser):
    """列出保洁员,用于管家办理退房时选择派单对象。"""
    if current_user["role"] not in (*_HANDLE_ROLES, "finance"):
        raise HTTPException(status_code=403, detail="无权查看员工列表")
    result = await db.execute(
        select(User)
        .where(User.role == UserRole.cleaner)
        .where(User.is_active == True)
        .order_by(User.display_name)
    )
    cleaners = result.scalars().all()
    return [
        CleanerBrief(user_id=u.user_id, display_name=u.display_name, phone=u.phone)
        for u in cleaners
    ]


async def _load_order_or_404(db, order_id: str) -> Order:
    row = (await db.execute(
        select(Order).where(Order.order_id == order_id, Order.is_deleted == False)
    )).scalar_one_or_none()
    if not row:
        raise HTTPException(status_code=404, detail="订单不存在")
    return row


@router.post("/orders/{order_id}/handle-checkin", response_model=HandleResult)
async def handle_checkin(
    order_id: str, body: HandleCheckinBody, db: DBSession, current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """办理入住: roomed_pending_checkin -> checked_in (或从更早状态直接推到 checked_in)。"""
    if current_user["role"] not in _HANDLE_ROLES:
        raise HTTPException(status_code=403, detail="无权办理入住")

    order = await _load_order_or_404(db, order_id)
    before_lock_snapshot = {
        "status": order.order_status.value,
        "note": order.notes,
    }
    # 快进入住走统一状态服务：末态/已取消守卫 + 房态联动都在里面（#183 收敛，
    # 不再手写 order_status / 复制房态循环 / 跨文件 import 私有函数）。
    # 单间入住（per-room checkin，镜像单间退房）：传 order_room_id 则只入住这一间；
    # 其余未入住房保持待入住。不传 = 整单入住（所有已排房、未入住的房一起入住）。
    from app.services.order_state import fast_checkin, checkin_one_room
    only_room_ids: Optional[list[str]] = None
    if body.order_room_id:
        all_rooms = (await db.execute(
            select(OrderRoom).where(OrderRoom.order_id == order.order_id)
        )).scalars().all()
        target = next((r for r in all_rooms if r.order_room_id == body.order_room_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="指定的房间不属于本订单")
        if target.room_id is None:
            raise HTTPException(status_code=400, detail="该房间尚未排房，无法办理入住")
        if target.checked_out_at is not None:
            raise HTTPException(status_code=400, detail="该房间已退房")
        if target.checked_in_at is not None:
            raise HTTPException(status_code=400, detail="该房间已办理入住")
        await checkin_one_room(db, order, target)
        only_room_ids = [target.room_id]
    else:
        await fast_checkin(db, order)
    if body.notes:
        order.notes = ((order.notes or "") + f"\n[入住] {body.notes}").strip()

    # 押金联动:管家现场收押金（押金 > 0 且未收时）。2026-06-05 王总流程。
    # 续住关联组：押金只收一次挂锚单(首段)；续住段入住时若锚单已收则跳过，不重复收。
    from app.services import stay_group as _stay_group
    _dep = await _stay_group.deposit_holder(db, order)
    deposit_collected = False
    if body.collect_deposit and (_dep.deposit or Decimal("0")) > 0 \
            and _dep.deposit_status == DepositStatus.not_collected:
        _dep.deposit_status = DepositStatus.collected
        deposit_collected = True

    if deposit_collected:
        await log_action_tx(
            db, current_user["user_id"], "deposit.collect", "order", order.order_id,
            notes="管家办理入住时收取押金",
        )
    apply_snapshot_manual_locks(
        order,
        before_lock_snapshot,
        {"status": order.order_status.value, "note": order.notes},
        source="human",
    )
    await log_action_tx(
        db, current_user["user_id"], "staff.handle_checkin", "order", order.order_id,
        notes=f"管家办理入住 by {current_user['user_id']}",
    )

    await db.commit()
    await db.refresh(order)

    # 后台化：下码(~3s 厂商往返)+发飞书密码卡/押金小票按钮卡移到 BackgroundTask，页面秒回，
    # 门锁码/卡片几秒后异步送达（对齐 orders.py transition 主路径 PR#222；之前这条 staff
    # 入住路径漏了后台化 → 前台"收押金并办理入住"一直转到卡片发完）。用 FastAPI
    # BackgroundTasks（响应后跑、开独立 session），不用 asyncio.create_task。lock_codes 改为
    # 异步送达，前端 CheckinDepositModal 不读它、只看成功即可。
    from app.services.lock.hooks import _process_checkin_codes
    background_tasks.add_task(_process_checkin_codes, order.order_id, only_room_ids)

    return HandleResult(
        order_id=order.order_id,
        order_status=order.order_status.value,
        lock_codes=None,
    )


@router.post("/orders/{order_id}/handle-checkout", response_model=HandleResult)
async def handle_checkout(
    order_id: str, body: HandleCheckoutBody, db: DBSession, current_user: CurrentUser,
    background_tasks: BackgroundTasks,
):
    """办理退房: checked_in -> pending_checkout,同时自动创建清扫任务派给指定保洁员。"""
    if current_user["role"] not in _HANDLE_ROLES:
        raise HTTPException(status_code=403, detail="无权办理退房")

    # 校验 cleaner（选填，批3 item1）：填了才校验并指派；留空 = 暂不派单，任务未分配。
    cleaner = None
    if body.cleaner_id:
        cleaner = (await db.execute(
            select(User).where(
                User.user_id == body.cleaner_id,
                User.role == UserRole.cleaner,
                User.is_active == True,
            )
        )).scalar_one_or_none()
        if not cleaner:
            raise HTTPException(status_code=404, detail="保洁员不存在或已禁用")

    order = await _load_order_or_404(db, order_id)
    before_lock_snapshot = {
        "status": order.order_status.value,
        "note": order.notes,
    }
    # pending_payment(已退房·待完成)是退房后的末态,不能再办理退房,否则会重复建保洁任务 (#41)
    if order.order_status in (
        OrderStatus.pending_checkout, OrderStatus.pending_payment, OrderStatus.completed,
    ):
        raise HTTPException(status_code=400, detail=f"订单已{order.order_status.value}")
    if order.order_status == OrderStatus.cancelled:
        raise HTTPException(status_code=400, detail="订单已取消")

    # 续住关联组：中间段（非末段）不许办退房——客人还没走，且撤码会撤掉共享门锁码把客人锁在外面，
    # 保洁费也只应在末段收。退房请到组内最后一段办（Task 11，与 orders.transition 同款闸）。
    from app.services import stay_group as _stay_group
    if not await _stay_group.is_group_last_segment(db, order):
        raise HTTPException(status_code=400,
            detail="这是续住组的中间段，客人还没走。退房请到组内最后一段办理。")

    # 2026-06-05 王总流程调整：退房不再卡收款（房费常是离店后/平台代收才到账）。
    # 收齐校验唯一收口移到「完成订单」步（orders.transition → completed）。
    # 退房只负责把客人送走 + 退押金 + 派清扫。

    # ── 单间退房：确定本次退哪些房，以及退完后订单是否清空（整单收尾）───────────
    # active_rooms：本单尚未退房的已排房行（checked_out_at IS NULL）。
    all_order_rooms = (await db.execute(
        select(OrderRoom).where(OrderRoom.order_id == order.order_id)
    )).scalars().all()
    active_rooms = [r for r in all_order_rooms if r.room_id and r.checked_out_at is None]

    if body.order_room_id:
        target = next((r for r in all_order_rooms if r.order_room_id == body.order_room_id), None)
        if target is None:
            raise HTTPException(status_code=404, detail="指定的房间不属于本订单")
        if target.room_id is None:
            raise HTTPException(status_code=400, detail="该房间尚未排房，无法退房")
        if target.checked_out_at is not None:
            raise HTTPException(status_code=400, detail="该房间已退房")
        checkout_rooms = [target]
    else:
        # 不传 = 整单退房：退掉全部在住房（无在住房则走待排房占位分支）。
        checkout_rooms = list(active_rooms)

    # 退完这批后，是否再无在住房 → 整单收尾（订单转 pending_checkout + 结算押金）。
    remaining_after = [r for r in active_rooms if r not in checkout_rooms]
    is_final = len(remaining_after) == 0
    checkout_now = now_cn()

    # 押金结算:已收押金且 > 0 时，按实退金额退/扣（不传 = 全退）。
    # 单间退房（非整单收尾）不结算押金——押金按整单，留到最后一间退房时结。
    # 续住关联组：押金结算认锚单(首段)那份，不是末段自己的（末段 deposit_status 恒 not_collected）。
    _dep = await _stay_group.deposit_holder(db, order)
    deposit_settled: Optional[dict] = None
    if is_final and _dep.deposit_status == DepositStatus.collected and (_dep.deposit or Decimal("0")) > 0:
        deposit_total = _dep.deposit
        refund = body.deposit_refund_amount if body.deposit_refund_amount is not None else deposit_total
        if refund < 0 or refund > deposit_total:
            raise HTTPException(status_code=400, detail=f"实退金额必须在 0 ~ {deposit_total:.2f} 之间")
        withhold = deposit_total - refund
        if withhold > 0 and not (body.deposit_withhold_reason and body.deposit_withhold_reason.strip()):
            raise HTTPException(status_code=400, detail="实退少于押金时必须填写扣款原因")
        _dep.deposit_returned = refund
        _dep.deposit_status = DepositStatus.withheld if refund == 0 else DepositStatus.returned
        if withhold > 0:
            meta = dict(_dep.metadata_) if _dep.metadata_ else {}
            meta["deposit_withhold_reason"] = body.deposit_withhold_reason.strip()
            _dep.metadata_ = meta
        deposit_settled = {
            "refund_amount": str(refund),
            "withhold_amount": str(withhold),
            "withhold_reason": body.deposit_withhold_reason if withhold > 0 else None,
        }

    # 状态流转：整单收尾才转 pending_checkout；单间退房（还剩在住房）订单保持 checked_in。
    if is_final:
        order.order_status = OrderStatus.pending_checkout
    if body.notes:
        order.notes = ((order.notes or "") + f"\n[退房] {body.notes}").strip()

    # 给本次退房的每间房打上退房时刻（per-room checkout 真相源）。
    for r in checkout_rooms:
        r.checked_out_at = checkout_now

    # 房态联动 + 派单:本次退的每间房都置 pending_clean 并各派一张清扫任务 (#42)
    # 单间退房只处理这一间，不碰订单里其它仍在住的房。
    room_ids = [r.room_id for r in checkout_rooms if r.room_id]
    tasks: list[Task] = []
    cleaned_room_ids: list[str] = []  # 实际转入 pending_clean 的房间（#116 保洁通知用）
    if room_ids:
        for rid in room_ids:
            room = (await db.execute(select(Room).where(Room.room_id == rid))).scalar_one_or_none()
            # 故障换房的旧房在 maintenance（或 locked）状态：客人未住、需维修，
            # 退房不该把它清扫释放，也不派清扫任务（否则维修态被冲、坏房回到可订池）。
            if room and room.room_status in (RoomStatus.maintenance, RoomStatus.locked):
                continue
            if room:
                room.room_status = RoomStatus.pending_clean
                cleaned_room_ids.append(rid)
            tasks.append(Task(
                task_id="TSK-" + uuid.uuid4().hex[:12].upper(),
                task_type=TaskType.cleaning,
                title=f"保洁 - 房间 {rid}",
                description=f"客人 {order.guest_name} 已退房,请及时清扫",
                order_id=order.order_id,
                room_id=rid,
                assignee_id=cleaner.user_id if cleaner else None,
                priority=TaskPriority.high,
                status=TaskStatus.pending,
                created_by=current_user["user_id"],
            ))
    else:
        # 待排房订单:无房间,仍建一张占位清扫任务
        tasks.append(Task(
            task_id="TSK-" + uuid.uuid4().hex[:12].upper(),
            task_type=TaskType.cleaning,
            title="保洁 - (待排房)",
            description=f"客人 {order.guest_name} 已退房,请及时清扫",
            order_id=order.order_id,
            room_id=None,
            assignee_id=cleaner.user_id if cleaner else None,
            priority=TaskPriority.high,
            status=TaskStatus.pending,
            created_by=current_user["user_id"],
        ))
    for t in tasks:
        db.add(t)
    # 全部房间都 locked/maintenance 时不派清扫任务，tasks 为空——退房仍须成功，
    # 不能 tasks[0] 崩成 500（真实事故：房态漂移的 locked 房 + 同步单 checked_in）。
    task = tasks[0] if tasks else None  # 兼容旧返回:created_task_id 取首张

    apply_snapshot_manual_locks(
        order,
        before_lock_snapshot,
        {"status": order.order_status.value, "note": order.notes},
        source="human",
    )

    await db.flush()
    for t in tasks:
        await db.refresh(t)

    # 重新 selectinload(rooms) 给 order_snapshot 用，否则 lazy load 在 async 上下文里炸
    from app.api.v1.orders import order_snapshot as _order_snapshot
    refreshed = (await db.execute(
        select(Order).where(Order.order_id == order.order_id).options(selectinload(Order.rooms))
    )).scalar_one()
    snap = _order_snapshot(refreshed)
    snap["_diff"] = {
        "cleaner_id": cleaner.user_id if cleaner else None,
        "task_id": task.task_id if task else None,
    }
    if deposit_settled:
        deposit_snap = _order_snapshot(refreshed)
        deposit_snap["_diff"] = deposit_settled
        await log_action_tx(
            db, current_user["user_id"], "deposit.return", "order", order.order_id,
            after_data=deposit_snap,
            notes="管家办理退房时结算押金",
        )
    await log_action_tx(
        db, current_user["user_id"], "staff.handle_checkout", "order", order.order_id,
        after_data=snap,
        notes=(f"管家办理退房并派给 {cleaner.display_name}" if cleaner
               else "管家办理退房（暂不派单，任务进待派池）"),
    )

    task_by_room = {t.room_id: t.task_id for t in tasks if t.room_id}

    await db.commit()

    # 后台化：撤客人码(~3s 厂商)+下保洁码(每房厂商往返)+发保洁群卡片(飞书)全移到
    # BackgroundTask，页面秒回（对齐入住/orders.py 主路径）。之前这段同步跑 → 前台
    # 「确认退房」一直转到全做完。状态流转/建清扫任务已同步提交，返回值不依赖门锁/飞书。
    # 单间退房：只撤本次退的这间房的客人码，别动订单里其它仍在住房的码。
    from app.services.lock.hooks import _process_checkout_codes
    background_tasks.add_task(
        _process_checkout_codes, order.order_id, cleaned_room_ids, task_by_room,
        room_ids,
    )

    return HandleResult(
        order_id=order.order_id,
        order_status=order.order_status.value,
        created_task_id=task.task_id if task else None,
    )


async def _notify_cleaning_best_effort(
    db, order: Order, cleaned_room_ids: list[str],
    task_by_room: dict[str, str] | None = None,
    cleaning_codes: dict[str, str] | None = None,
) -> None:
    """退房→pending_clean 的保洁群通知钩子（#116）。

    硬约束：best-effort。退房是核心业务，通知是附属——构造或发送失败一律只
    记日志，绝不抛异常、绝不阻断退房或房态流转。整个函数体裹在 try/except 里。

    内容：每间转入 pending_clean 的房，发一条「房间号 · 退房时间 · 房型」；
    若同房当天有新入住（turnaround）则追加「⚡ 今日有新客，优先打扫」。
    应用模式下把发卡返回的 message_id 存进 metadata_['feishu_card_message_ids']，完工变灰用。

    去重：同一房间同次退房只发一次。handle_checkout 在 order_status 已是
    pending_checkout 时直接 400，天然保证一张订单只走到这里一次；这里再用
    order.metadata_['cleaning_notified'] 记下已通知的房间，作显式幂等兜底。
    """
    try:
        if not cleaned_room_ids:
            return

        from app.services.feishu_lead_alert import send_cleaning_alert

        meta = dict(order.metadata_) if order.metadata_ else {}
        already = set(meta.get("cleaning_notified") or [])

        # 卡片给保洁看的是墙上钟：必须北京时间（曾用 UTC，12:05 显示成 04:05）
        checkout_dt = now_cn()
        checkout_str = checkout_dt.strftime("%Y-%m-%d %H:%M")

        newly_notified: list[str] = []
        card_ids: dict[str, dict] = dict(meta.get("feishu_card_message_ids") or {})
        for rid in cleaned_room_ids:
            if rid in already:
                continue  # 去重：本房本次退房已通知过

            room = (await db.execute(
                select(Room).where(Room.room_id == rid)
            )).scalar_one_or_none()
            room_name = room.room_name if room else rid
            room_type = (room.room_type if room and room.room_type else "—")

            # turnaround 判定基准日：本订单本房的退房日（客人离店那天）。新客若同日
            # 入住即接力。用退房日而非 wall-clock now —— 提前/补办退房时仍判定正确。
            checkout_day = await _order_room_checkout_date(db, order.order_id, rid)
            next_guest = (
                await _same_day_next_guest(db, rid, checkout_day, order.order_id)
                if checkout_day is not None else None
            )
            turnaround = next_guest is not None

            message_id = await send_cleaning_alert(
                room_id=rid,
                room_name=room_name,
                checkout_time=checkout_str,
                room_type=room_type,
                turnaround=turnaround,
                next_guest=next_guest,
                task_id=(task_by_room or {}).get(rid),
                cleaning_code=(cleaning_codes or {}).get(rid),
                trial_tag=trial_tag_for_notes(order.notes),
            )
            # 应用模式发卡成功才有 message_id；存下来供完工时原地改灰
            # （见 services/cleaning.grey_cleaning_card_best_effort）。
            if message_id:
                card_ids[rid] = {"message_id": message_id, "checkout_time": checkout_str}
            newly_notified.append(rid)

        if newly_notified:
            meta["cleaning_notified"] = list(already | set(newly_notified))
            if card_ids:
                meta["feishu_card_message_ids"] = card_ids
            order.metadata_ = meta
            await db.commit()
    except Exception as e:  # noqa: BLE001 — best-effort，吞掉一切
        logger.warning(
            "Cleaning alert hook failed (checkout unaffected): %s: %s",
            type(e).__name__, str(e)[:120],
        )


async def _order_room_checkout_date(db, order_id: str, room_id: str):
    """取本订单本房的退房日（order_rooms 是真相源；回退顶层 orders）。"""
    day = (await db.execute(
        select(OrderRoom.check_out_date).where(
            OrderRoom.order_id == order_id,
            OrderRoom.room_id == room_id,
        ).limit(1)
    )).scalar_one_or_none()
    if day is None:
        day = (await db.execute(
            select(Order.check_out_date).where(Order.order_id == order_id)
        )).scalar_one_or_none()
    return day


async def _has_same_day_checkin(
    db, room_id: str, day, exclude_order_id: str
) -> bool:
    """同房当天是否有另一张订单入住（turnaround 判定）。

    查 order_rooms × orders：同房、入住日 == day、非取消、非删除、且不是当前
    退房这张单本身。命中即 turnaround，保洁需优先打扫。"""
    row = (await db.execute(
        select(OrderRoom.order_room_id)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .where(
            OrderRoom.room_id == room_id,
            OrderRoom.check_in_date == day,
            OrderRoom.order_id != exclude_order_id,
            Order.is_deleted == False,
            Order.order_status.not_in([OrderStatus.cancelled]),
        )
        .limit(1)
    )).scalar_one_or_none()
    return row is not None


async def _same_day_next_guest(db, room_id: str, day, exclude_order_id: str) -> str | None:
    """同房当天下一位入住客人的姓名（turnaround 卡片展示用），无则 None。
    取最早入住的那单（多单时优先催最急的）。"""
    row = (await db.execute(
        select(Order.guest_name)
        .join(OrderRoom, OrderRoom.order_id == Order.order_id)
        .where(
            OrderRoom.room_id == room_id,
            OrderRoom.check_in_date == day,
            OrderRoom.order_id != exclude_order_id,
            Order.is_deleted == False,
            Order.order_status.not_in([OrderStatus.cancelled]),
        )
        .order_by(Order.created_at.asc())
        .limit(1)
    )).scalar_one_or_none()
    return row or None


@router.post("/orders/{order_id}/revert-checkout", response_model=RevertCheckoutResult)
async def revert_checkout(
    order_id: str,
    body: RevertCheckoutBody,
    db: DBSession,
    current_user: CurrentUser,
):
    """撤销退房: completed → checked_in。

    用于店长发现已完成订单需要修正的场景 (信息错误、补录收款后想重走流程)。
    副作用:
      - 关联的 pending / in_progress 清扫任务全部 cancelled (避免保洁去打扫还没退房的房间)
      - 房间状态: 若当前 available 且无冲突订单 → 恢复 occupied;
        若已被新订单 reserve / occupied,则不强占,业务真实优先
      - audit log 必带 reason
    撤销退房不走 transition_status 的 VALID_TRANSITIONS (completed 是 terminal),
    使用本专用 endpoint 才能跨越终态边界。
    """
    if current_user["role"] not in _HANDLE_ROLES:
        raise HTTPException(status_code=403, detail="无权撤销退房")

    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="撤销退房必须填写原因")

    order = await _load_order_or_404(db, order_id)
    if order.order_status != OrderStatus.completed:
        raise HTTPException(
            status_code=400,
            detail=f"只能撤销已完成订单，当前状态：{order.order_status.value}",
        )

    before_status = order.order_status.value
    order.order_status = OrderStatus.checked_in

    # 房态恢复: 单房 + 多房都遍历 order_rooms 收集所有已排房间
    from app.models.order_room import OrderRoom
    or_rows = (await db.execute(
        select(OrderRoom).where(
            OrderRoom.order_id == order.order_id,
            OrderRoom.room_id.isnot(None),
        )
    )).scalars().all()
    room_restored = False
    for or_row in or_rows:
        room = (await db.execute(
            select(Room).where(Room.room_id == or_row.room_id)
        )).scalar_one_or_none()
        if not room:
            continue
        if room.room_status == RoomStatus.available:
            has_conflict = await check_room_conflict(
                db, room.room_id, or_row.check_in_date, or_row.check_out_date,
                exclude_order_id=order_id,
            )
            if not has_conflict:
                room.room_status = RoomStatus.occupied
                room_restored = True

    # 取消活跃清扫任务
    cancelled_task_ids: list[str] = []
    tasks_result = await db.execute(
        select(Task).where(
            Task.order_id == order.order_id,
            Task.task_type == TaskType.cleaning,
            Task.status.in_([TaskStatus.pending, TaskStatus.in_progress]),
        )
    )
    for t in tasks_result.scalars().all():
        t.status = TaskStatus.cancelled
        cancelled_task_ids.append(t.task_id)

    # 清掉本次退房留下的保洁通知痕迹：cleaning_notified（去重闸）+
    # feishu_card_message_ids（灰卡句柄）。否则重新退房时去重闸会拦掉新卡不发，
    # 且旧 message_id 会被下一轮完工误 PATCH（陈旧卡）。整单回滚→整单清。
    if order.metadata_:
        meta = dict(order.metadata_)
        changed = False
        for k in ("cleaning_notified", "feishu_card_message_ids"):
            if k in meta:
                del meta[k]
                changed = True
        if changed:
            order.metadata_ = meta

    apply_snapshot_manual_locks(
        order,
        {"status": before_status},
        {"status": order.order_status.value},
        source="human",
    )
    await db.flush()
    from app.api.v1.orders import order_snapshot as _order_snapshot
    refreshed = (await db.execute(
        select(Order).where(Order.order_id == order.order_id).options(selectinload(Order.rooms))
    )).scalar_one()
    snap = _order_snapshot(refreshed)
    snap["_diff"] = {
        "before_status": before_status,
        "cancelled_task_ids": cancelled_task_ids,
        "room_restored": room_restored,
        "reason": reason,
    }
    await log_action_tx(
        db, current_user["user_id"], "staff.revert_checkout", "order", order.order_id,
        after_data=snap,
        notes=f"撤销退房原因：{reason}",
    )
    await db.commit()

    return RevertCheckoutResult(
        order_id=order.order_id,
        order_status=order.order_status.value,
        cancelled_task_ids=cancelled_task_ids,
        room_restored=room_restored,
    )


@router.post("/orders/{order_id}/revert-checkin", response_model=RevertCheckinResult)
async def revert_checkin(
    order_id: str,
    body: RevertCheckinBody,
    db: DBSession,
    current_user: CurrentUser,
):
    """撤销入住（批3 item5）：checked_in → roomed_pending_checkin。

    用于「误点入住」——前台手滑把还没到店的客人办成在住。撤回状态并把房态
    occupied → reserved（只回退本单自己占的房；若房已被别的单占用则不强改）。
    checked_in 是 VALID_TRANSITIONS 的前进态、无回退边，故用本专用 endpoint 跨越。
    完整 undo 办理入住的副作用（评审 F1-F4）：
      - 状态：有排房→roomed_pending_checkin，未排房→paid_pending_room
        （按实际排房派生，避免"待入住却无房"的非法态；#评审 F1）
      - 房态：本单占的房 occupied→reserved，但只在**没有别的未取消订单占用该房**时才回退，
        防止换房/stale room_id 把别的在住客人的房释放（#评审 F3；用 order_room_ids 含 legacy room_id）
      - 门锁码：撤回办理入住时下发的客人码（误点入住客人不该保有门禁；#评审 F2），fail-safe 不回滚状态
      - 押金：办理入住自动置 collected 的押金回退为 not_collected（误点入住并未真实收款；#评审 F4）
    reason 必填进 audit。⚠️ 撤门锁码/回退押金是动锁动钱行为，行为口径请王总过目。
    """
    if current_user["role"] not in _HANDLE_ROLES:
        raise HTTPException(status_code=403, detail="无权撤销入住")

    reason = (body.reason or "").strip()
    if not reason:
        raise HTTPException(status_code=400, detail="撤销入住必须填写原因")

    order = await _load_order_or_404(db, order_id)
    if order.order_status != OrderStatus.checked_in:
        raise HTTPException(
            status_code=400,
            detail=f"只能撤销在住订单，当前状态：{order.order_status.value}",
        )

    before_status = order.order_status.value

    # 目标状态按实际排房派生（评审 F1）：从更早状态 fast-checkin 的无房单不能硬塞
    # roomed_pending_checkin（那态语义=已排房待入住），否则造出"待入住却无房"的非法态。
    from app.services.order_state import order_room_ids
    room_ids = await order_room_ids(db, order)
    order.order_status = (
        OrderStatus.roomed_pending_checkin if room_ids else OrderStatus.paid_pending_room
    )

    # 房态回退（评审 F3 + legacy room_id）：只回退本单占且**无其它订单占用**的房，
    # 避免把换房/stale room_id 场景下别的在住客人正住的房误释放→重复预订。
    room_reverted = False
    for rid in room_ids:
        room = (await db.execute(select(Room).where(Room.room_id == rid))).scalar_one_or_none()
        if not room or room.room_status != RoomStatus.occupied:
            continue
        has_conflict = await check_room_conflict(
            db, rid, order.check_in_date, order.check_out_date, exclude_order_id=order_id,
        )
        if has_conflict:
            continue  # 该房已被别的未取消订单占用，不强改
        room.room_status = RoomStatus.reserved
        room_reverted = True

    # 押金回退（评审 F4）：办理入住时自动记的押金收取，误点撤销时一并撤回（并未真实收款）。
    deposit_reverted = False
    if order.deposit_status == DepositStatus.collected:
        order.deposit_status = DepositStatus.not_collected
        deposit_reverted = True

    apply_snapshot_manual_locks(
        order,
        {"status": before_status},
        {"status": order.order_status.value},
        source="human",
    )
    await db.flush()
    from app.api.v1.orders import order_snapshot as _order_snapshot
    refreshed = (await db.execute(
        select(Order).where(Order.order_id == order.order_id).options(selectinload(Order.rooms))
    )).scalar_one()
    snap = _order_snapshot(refreshed)
    snap["_diff"] = {
        "before_status": before_status, "room_reverted": room_reverted,
        "deposit_reverted": deposit_reverted, "reason": reason,
    }
    await log_action_tx(
        db, current_user["user_id"], "staff.revert_checkin", "order", order.order_id,
        after_data=snap,
        notes=f"撤销入住原因：{reason}",
    )
    await db.commit()

    # 门锁码撤回（评审 F2）：fail-safe，锁/厂商出问题绝不回滚已提交的状态回退。
    from app.services.lock.hooks import revoke_codes_on_checkout
    await revoke_codes_on_checkout(db, refreshed)

    return RevertCheckinResult(
        order_id=order.order_id,
        order_status=order.order_status.value,
        room_reverted=room_reverted,
        deposit_reverted=deposit_reverted,
    )
