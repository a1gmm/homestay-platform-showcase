"""保洁完工流转——「打扫完了」一键直达可用（飞书回调 / 未来其它入口共用）。

与 tasks.review_task（管家「查房通过」）效果一致：清扫任务→done + 房间→available
+ 订单 cleaning_status=inspected，但**不强制 submit→review 握手**，且**幂等**
（重复点击安全）。语义：保洁点一下即完工，不设查房环节（用户 2026-06-30 拍板）。
不触发订单完成——订单完成「收齐房费」的唯一收口仍在 orders /transition（见 #40）。
"""
import logging
from datetime import datetime, timezone
from uuid import uuid4

from fastapi import BackgroundTasks
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.cleaning_pricing import OWNER_SELF_CHECKOUT_CLEANING_FEE
from app.core.datetime_helpers import now_cn
from app.core.trial_tags import trial_tag_for_notes
from app.services import service_fee_ledger
from app.services.audit import log_action_tx
from app.services.service_fees import ServiceFees, get_service_fees
from app.models.expense import Expense, ExpenseCategory, ExpensePayer
from app.models.order import Order, OrderStatus, CleaningStatus, is_owner_self_order
from app.models.order_room import OrderRoom
from app.models.room import Room, RoomStatus
from app.models.task import Task, TaskType, TaskStatus, ReviewStatus

logger = logging.getLogger(__name__)

# 退房后房间可能处于的「脏/占」态，验收/完工时一并恢复为 available。
_RESTORABLE = (
    RoomStatus.occupied, RoomStatus.reserved,
    RoomStatus.pending_clean, RoomStatus.cleaning,
)


def _add_service_expense(
    db: AsyncSession, *, room: Room, order_id: str,
    category: ExpenseCategory, amount, description: str, expense_date,
    payer: ExpensePayer,
) -> None:
    """记一条自动服务费（is_service_fee=True）。"""
    db.add(
        Expense(
            expense_id="EXP-" + uuid4().hex[:12].upper(),
            category=category,
            amount=amount,
            description=description,
            expense_date=expense_date,
            room_id=room.room_id,
            order_id=order_id,
            payer=payer,
            owner_id=room.owner_id,
            is_service_fee=True,
        )
    )


async def _final_checkout_date(
    db: AsyncSession,
    order_id: str,
    room_id: str,
):
    """Return the accounting date for checkout fees, including continuations."""
    order = await db.scalar(select(Order).where(Order.order_id == order_id))
    if order is None:
        return None
    stmt = select(func.max(OrderRoom.check_out_date)).join(
        Order, Order.order_id == OrderRoom.order_id
    )
    if order.stay_group_id:
        stmt = stmt.where(
            Order.stay_group_id == order.stay_group_id,
            Order.is_deleted.is_(False),
            Order.order_status != OrderStatus.cancelled,
            OrderRoom.room_id == room_id,
        )
    else:
        stmt = stmt.where(
            OrderRoom.order_id == order_id,
            OrderRoom.room_id == room_id,
        )
    return await db.scalar(stmt) or order.check_out_date


async def _room_nights(db: AsyncSession, order_id: str, room_id: str | None) -> int:
    """该房的日耗间夜数。

    续住组：日耗每晚都在消耗，而退房打扫只在末段触发一次——只数末段夜数会把
    住 11 晚的续住客按 1 晚收（业主被少收）。故续住组按**整组活段**里落在这间房
    的夜数加总（同房续住数所有段；跨房只数这间房的段，各归各房东）。已取消段不算。
    保洁 65 / 洗涤 15 不走这里，仍一趟一次、不受影响。
    无组则本单该房日期，回退订单级日期。
    """
    order = (await db.execute(
        select(Order).where(Order.order_id == order_id)
    )).scalar_one_or_none()

    if order is not None and order.stay_group_id and room_id:
        from app.services import stay_group as _stay_group
        members = await _stay_group.operational_group_orders(db, order)
        alive_ids = [
            o.order_id for o in members
            if not o.is_deleted and o.order_status != OrderStatus.cancelled
        ]
        if alive_ids:
            rooms = (await db.execute(
                select(OrderRoom).where(
                    OrderRoom.order_id.in_(alive_ids), OrderRoom.room_id == room_id
                )
            )).scalars().all()
            total = sum(
                max((r.check_out_date - r.check_in_date).days, 0)
                for r in rooms if r.check_in_date and r.check_out_date
            )
            if total > 0:
                return total

    if room_id:
        orm = (await db.execute(
            select(OrderRoom).where(
                OrderRoom.order_id == order_id, OrderRoom.room_id == room_id
            )
        )).scalars().first()
        if orm is not None and orm.check_in_date and orm.check_out_date:
            return max((orm.check_out_date - orm.check_in_date).days, 0)
    if order is not None and order.check_in_date and order.check_out_date:
        return max((order.check_out_date - order.check_in_date).days, 0)
    return 0


async def add_checkout_cleaning_charge(
    db: AsyncSession, *, room: Room, order_id: str | None
) -> bool:
    """退房打扫 → 向房东一次生成 3 笔服务费的**单一构造点**：
    保洁(65/间) + 洗涤(15/间) + 日耗(22/间/夜)。费率取 service_fee_config（可配置）。

    两条完成路径共用：飞书「打扫完了」(complete_cleaning_for_task) 与
    管家「查房通过」(api/v1/tasks.review_task)。二者互斥——谁先把清扫任务置 done，
    另一条要么撞 review_status 门 400、要么原子判定 already_done，绝不会两条都记账。
    **调用方负责只在「未完成→完成」那一次转变时调用一次**。返回是否真的记了账。

    洗涤按「每间一笔」——多房单每间各走一次本函数，自然实现 15×间数；续住合并单
    只在最终退房走一次，自然「只收一次洗涤」。日耗按该房间夜数计。费率为 0 的项跳过。
    """
    if not settings.CHECKOUT_CLEANING_CHARGE_ENABLED:
        return False
    if not order_id or not room.owner_id:
        # 缺业主或订单 → 无人可计费；留痕免得成静默收入缺口（评审 Finding 5）。
        logger.info(
            "退房打扫未计保洁费(缺 owner/order): room=%s owner=%s order=%s",
            room.room_id, room.owner_id, order_id,
        )
        return False

    fees: ServiceFees = await get_service_fees(db)
    order = await db.scalar(select(Order).where(Order.order_id == order_id))
    owner_self = order is not None and is_owner_self_order(order)
    payer = ExpensePayer.company if owner_self else ExpensePayer.owner
    checkout_cleaning_fee = (
        OWNER_SELF_CHECKOUT_CLEANING_FEE if owner_self else fees.checkout_cleaning_fee
    )
    beds = room.beds
    nights = await _room_nights(db, order_id, room.room_id)
    expense_date = await _final_checkout_date(db, order_id, room.room_id)
    if expense_date is None:
        logger.warning(
            "退房打扫未计服务费(缺最终退房日): room=%s order=%s",
            room.room_id,
            order_id,
        )
        return False
    consumable = fees.consumable_fee_per_room_night * nights
    candidates = (
        (
            ExpenseCategory.cleaning,
            checkout_cleaning_fee,
            f"退房打扫 {room.room_name}",
        ),
        (
            ExpenseCategory.laundry,
            fees.laundry_fee_per_room * beds,
            f"洗涤费 {room.room_name}" + (f" ×{beds}床" if beds != 1 else ""),
        ),
        (
            ExpenseCategory.daily_supplies,
            consumable,
            f"日耗品 {room.room_name} {nights}晚",
        ),
    )

    await service_fee_ledger.lock_owner_service_fee_ledger(db, room.owner_id)
    existing_rows = (
        await db.execute(
            select(Expense.category, Expense.expense_date).where(
                Expense.order_id == order_id,
                Expense.room_id == room.room_id,
                Expense.is_service_fee.is_(True),
                Expense.is_deleted.is_(False),
                service_fee_ledger.checkout_service_fee_identity_clause(),
            )
        )
    ).all()
    wrong_month = tuple(
        (category, existing_date)
        for category, existing_date in existing_rows
        if existing_date is None
        or (existing_date.year, existing_date.month)
        != (expense_date.year, expense_date.month)
    )
    if wrong_month:
        raise service_fee_ledger.CheckoutServiceFeeWrongMonthError(
            order_id=order_id,
            room_id=room.room_id,
            expected_date=expense_date,
            conflicts=wrong_month,
        )
    existing_categories = {category for category, _ in existing_rows}
    for category, amount, description in candidates:
        if amount <= 0 or category in existing_categories:
            continue
        _add_service_expense(
            db,
            room=room,
            order_id=order_id,
            category=category,
            amount=amount,
            description=description,
            expense_date=expense_date,
            payer=payer,
        )
    return True


async def _revoke_cleaning_code_bg(room_id: str, order_id: str | None) -> None:
    """应答后撤保洁码：请求会话在后台任务执行前已被 FastAPI 关闭，须新开会话。

    fail-safe：任何异常只记日志（hook 内部本就吞异常，这里兜的是建会话本身）。
    """
    try:
        from app.core.database import AsyncSessionLocal
        from app.services.lock.hooks import revoke_cleaning_code_on_done

        async with AsyncSessionLocal() as session:
            await revoke_cleaning_code_on_done(session, room_id, order_id=order_id)
    except Exception:  # noqa: BLE001 — 尽力而为，绝不影响已应答的完工
        logger.warning("后台撤保洁码失败 room=%s", room_id, exc_info=True)


async def start_cleaning_for_task(
    db: AsyncSession,
    *,
    task_id: str | None,
    room_id: str | None,
    operator_open_id: str,
) -> dict:
    """保洁点【打扫卫生】：串行门闩 + 记录开始（幂等）。

    门闩范围＝按 open_id 各自一间：若该 open_id 名下已有「已开始（cleaning_started_at
    非空）且未 done」的清扫任务，且不是本任务 → blocked，不改任何状态。
    否则把本任务标为已开始（cleaning_started_by=open_id, cleaning_started_at=now）。
    不碰房态、不碰计费。返回 {"task_found","room_name","room_id","order_id",
    "guest_name","blocked","busy_room_name","already_started"}。
    """
    now = datetime.now(timezone.utc)
    task: Task | None = None
    if task_id:
        task = (await db.execute(select(Task).where(Task.task_id == task_id))).scalar_one_or_none()

    if task is None or task.task_type != TaskType.cleaning:
        return {"task_found": False, "room_name": None, "room_id": room_id,
                "order_id": None, "guest_name": None, "blocked": False,
                "busy_room_name": None, "already_started": False}

    # 门闩：同一 open_id 有别的「已开始且未终结」清扫任务 → 拦下。终态含 done 与
    # cancelled——若只排除 done，一张「已开始又被取消」的任务会永久占着门闩，
    # 把这个保洁锁死在别的房外面（评审 P1）。
    busy = (await db.execute(
        select(Task).where(
            Task.task_type == TaskType.cleaning,
            Task.status.notin_((TaskStatus.done, TaskStatus.cancelled)),
            Task.cleaning_started_by == operator_open_id,
            Task.cleaning_started_at.is_not(None),
            Task.task_id != task.task_id,
        ).limit(1)
    )).scalar_one_or_none()
    if busy is not None:
        busy_room = (await db.execute(
            select(Room).where(Room.room_id == busy.room_id)
        )).scalar_one_or_none() if busy.room_id else None
        return {"task_found": True, "room_name": None, "room_id": task.room_id,
                "order_id": task.order_id, "guest_name": None, "blocked": True,
                "busy_room_name": (busy_room.room_name if busy_room else busy.room_id),
                "already_started": False}

    already_started = task.cleaning_started_at is not None
    if not already_started:
        task.cleaning_started_by = operator_open_id
        task.cleaning_started_at = now

    # 下码/发码只认任务自身绑定的房，不回退到回调里带的 room_id——避免伪造回调
    # 把 payload room_id 塞进下码路径给任意房开锁（评审信任边界项）。
    room_name = None
    target_room_id = task.room_id
    if target_room_id:
        room = (await db.execute(
            select(Room).where(Room.room_id == target_room_id)
        )).scalar_one_or_none()
        room_name = room.room_name if room is not None else target_room_id

    guest_name = None
    if task.order_id:
        order = (await db.execute(
            select(Order).where(Order.order_id == task.order_id)
        )).scalar_one_or_none()
        guest_name = order.guest_name if order is not None else None

    await log_action_tx(
        db, None, "task.cleaning_started_feishu", "task", task.task_id,
        notes=f"飞书领取打扫 open_id={operator_open_id}",
    )
    await db.commit()

    return {"task_found": True, "room_name": room_name, "room_id": target_room_id,
            "order_id": task.order_id, "guest_name": guest_name, "blocked": False,
            "busy_room_name": None, "already_started": already_started}


async def reset_cleaning_started(db: AsyncSession, *, task_id: str) -> None:
    """清掉某清扫任务的「已开始」门闩标记（未完成才清），让保洁能重点【打扫卫生】。

    用于领取扇出失败兜底：`start_cleaning_for_task` 先标已开始再后台下码/发码，
    若扇出失败（锁离线/私聊失败）保洁会「已开始但没码、且被门闩锁死在别的房外」，
    清掉标记即可重试（评审 P1）。best-effort 语义由调用方兜。
    """
    if not task_id:
        return
    task = (await db.execute(select(Task).where(Task.task_id == task_id))).scalar_one_or_none()
    if task is not None and task.status != TaskStatus.done:
        task.cleaning_started_by = None
        task.cleaning_started_at = None
        await db.commit()


async def complete_cleaning_for_task(
    db: AsyncSession,
    *,
    task_id: str | None,
    room_id: str | None,
    operator_open_id: str = "",
    background_tasks: BackgroundTasks | None = None,
) -> dict:
    """把一张清扫任务标记完工并恢复房间可用（幂等）。

    优先按 task_id 定位任务（精确）；任务上的 room_id 优先，回退到回调带的 room_id。
    返回 {"room_name", "room_id", "order_id", "already_done", "guest_name", "deposit",
    "task_found", "has_order"}，供调用方拼应答文案及退押金提醒（guest_name/deposit 取自
    关联订单；room_id/order_id 供退押金卡下发/撤销查房码用）。
    task_found/has_order 供调用方判定：都没解析到时别回假成功、没订单时别发空的
    退押金提醒（task 永远未命中 → already_done 恒 False，每次点击/重试都会重发）。

    already_done 来自「pending→done」的原子条件 UPDATE（WHERE status != done）：
    两人同时点 / 飞书重试撞上慢请求时，只有赢家拿到 already_done=False，
    退押金提醒不会重发。读-改-写的 ORM 写法在并发下两边都会读到未完成。

    传 background_tasks 时撤保洁码排进应答后执行（锁供应商超时上限 12s，
    飞书回调要求 3s 内应答）；不传（管家 UI 等非限时入口）保持同步撤码。
    """
    now = datetime.now(timezone.utc)
    task: Task | None = None
    if task_id:
        task = (await db.execute(select(Task).where(Task.task_id == task_id))).scalar_one_or_none()

    already_done = False
    target_room_id = room_id
    if task is not None and task.task_type == TaskType.cleaning:
        res = await db.execute(
            update(Task)
            .where(Task.task_id == task.task_id, Task.status != TaskStatus.done)
            .values(
                status=TaskStatus.done,
                review_status=ReviewStatus.approved.value,
                completed_at=func.coalesce(Task.completed_at, now),
                reviewed_at=func.coalesce(Task.reviewed_at, now),
            )
        )
        already_done = (res.rowcount or 0) == 0
        target_room_id = task.room_id or room_id

    room_name: str | None = None
    if target_room_id:
        room = (await db.execute(select(Room).where(Room.room_id == target_room_id))).scalar_one_or_none()
        if room is not None:
            room_name = room.room_name
            if room.room_status in _RESTORABLE:
                room.room_status = RoomStatus.available

            # 退房打扫完成 → 自动向房东计一笔保洁费（60，房东全担）。
            # 幂等：只在本次真正完工（already_done=False 的赢家）时记；飞书重试 /
            # 两人同点时 already_done=True，不会重复扣。走共用构造点，与查房通过路径同源。
            if not already_done and task is not None and task.task_type == TaskType.cleaning:
                try:
                    await add_checkout_cleaning_charge(db, room=room, order_id=task.order_id)
                except service_fee_ledger.CheckoutServiceFeeWrongMonthError:
                    await db.rollback()
                    raise

    guest_name: str | None = None
    deposit = None
    has_order = False
    trial_tag: str | None = None
    if task is not None and task.order_id:
        order = (await db.execute(select(Order).where(Order.order_id == task.order_id))).scalar_one_or_none()
        if order is not None:
            has_order = True
            guest_name = order.guest_name
            deposit = order.deposit
            trial_tag = trial_tag_for_notes(order.notes)
            if order.order_status not in (OrderStatus.cancelled, OrderStatus.completed):
                order.cleaning_status = CleaningStatus.inspected

    # 审计与完工回写同事务提交（写失败则整体回滚）。移进本服务后所有「打扫完了」
    # 调用方（飞书卡片/ws）复用；task_id 可能为空（回调只带 room_id），仍留痕。
    await log_action_tx(
        db, None, "task.cleaning_done_feishu", "task", task_id,
        notes=f"飞书一键完工 open_id={operator_open_id}",
    )
    await db.commit()

    # 保洁完工即撤该房保洁码（delKey），不留口子——兜底还有次日12:00过期（方案 B）。
    # fail-safe：hook 内部吞掉一切异常并回滚自身，绝不影响已提交的完工回写。
    if target_room_id:
        order_id_for_revoke = task.order_id if task else None
        if background_tasks is not None:
            background_tasks.add_task(
                _revoke_cleaning_code_bg, target_room_id, order_id_for_revoke
            )
        else:
            from app.services.lock.hooks import revoke_cleaning_code_on_done
            await revoke_cleaning_code_on_done(
                db, target_room_id, order_id=order_id_for_revoke
            )

    # 完工落库后改灰群卡（best-effort，改卡失败不影响已提交的完工状态）。
    # 仅首次完工触发：重复点击不再 PATCH，避免覆盖灰卡上的原始扫完时间
    # （与回调链里退押金提醒的 already_done 守卫同一模式）。
    if not already_done:
        await grey_cleaning_card_best_effort(db, task)

    return {
        "room_name": room_name,
        "room_id": target_room_id,
        "order_id": task.order_id if task else None,
        "already_done": already_done,
        "guest_name": guest_name,
        "deposit": deposit,
        "task_found": task is not None and task.task_type == TaskType.cleaning,
        "has_order": has_order,
        "trial_tag": trial_tag,
    }


async def grey_cleaning_card_best_effort(db: AsyncSession, task: Task | None) -> None:
    """完工后把保洁群原蓝卡原地改灰（方案D，两个终态入口共用）。

    message_id 在退房发卡时写进 Order.metadata_["feishu_card_message_ids"][room_id]
    （见 staff_portal._notify_cleaning_best_effort）。webhook 降级模式没有 message_id，
    自然跳过。硬约束：best-effort——任何失败只 log，绝不影响完工回写/查房应答。
    """
    try:
        if task is None or task.task_type != TaskType.cleaning:
            return
        if not (task.room_id and task.order_id):
            return
        order = (await db.execute(
            select(Order).where(Order.order_id == task.order_id)
        )).scalar_one_or_none()
        entry = (
            ((order.metadata_ or {}).get("feishu_card_message_ids") or {}).get(task.room_id)
            if order is not None else None
        )
        if not entry or not entry.get("message_id"):
            return
        room = (await db.execute(
            select(Room).where(Room.room_id == task.room_id)
        )).scalar_one_or_none()

        from app.services.feishu_lead_alert import update_cleaning_card_done

        await update_cleaning_card_done(
            message_id=entry["message_id"],
            room_id=task.room_id,
            room_name=(room.room_name if room is not None else task.room_id),
            checkout_time=entry.get("checkout_time") or "",
            done_time=now_cn().strftime("%H:%M"),  # 展示走北京时间，UTC 会差 8 小时
            trial_tag=trial_tag_for_notes(order.notes if order is not None else None),
        )
    except Exception as e:  # noqa: BLE001 — best-effort，吞掉一切
        logger.warning(
            "Grey cleaning card failed (completion unaffected): %s: %s",
            type(e).__name__, str(e)[:120],
        )
