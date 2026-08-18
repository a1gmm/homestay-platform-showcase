"""
Celery background tasks for Phase 1.
Phase 2 lock tasks (lock_tasks.py) will be added when Huohe API is integrated.
"""
import logging
from datetime import date, datetime, timezone, timedelta
from app.core.datetime_helpers import today_cn
from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.workers.celery_app import celery_app
from app.workers.async_helper import run_async
from app.workers.daily_guard import claim_daily_run, release_daily_run
from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.order import Order, OrderStatus
from app.models.task import Task, TaskStatus
from app.services.feishu_lead_alert import send_checkin_alert, send_task_alert, send_checkout_alert

logger = logging.getLogger(__name__)

# 每日任务的统一重试策略：claim 后任务主体失败会 release 当日 slot 并抛出异常，
# 由 celery autoretry 指数退避重试(60s/120s/240s)。不配 autoretry 的话 release
# 只是把 slot 让给「下一次 beat 触发」——而每日任务当天不会再触发。
DAILY_RETRY_KW = dict(
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=600,
    max_retries=3,
)


async def _check_overdue_tasks_async(db) -> bool:
    """逾期任务核心逻辑。每个 CN 自然日最多推送一次（防 beat 重启刷屏）。

    返回 True 表示本次推送了飞书，False 表示今日已发过或无逾期任务而跳过。
    """
    if not await claim_daily_run(db, "check_overdue_tasks"):
        return False

    try:
        result = await db.execute(
            select(Task).where(
                Task.deadline < datetime.now(timezone.utc),
                Task.status != TaskStatus.done,
            )
        )
        overdue = result.scalars().all()
        if not overdue:
            return False

        titles = [t.title for t in overdue[:10]]
        logger.warning("共 %d 个逾期任务: %s", len(overdue), titles)
        lines = [f"共 {len(overdue)} 个任务已逾期，请尽快处理"]
        lines += [f"· {t}" for t in titles]
        if len(overdue) > len(titles):
            lines.append(f"…等共 {len(overdue)} 个")
        await send_task_alert(
            "⏰ 逾期任务提醒",
            lines,
            button_text="去处理",
            button_url=f"{settings.FRONTEND_BASE_URL}/tasks",
        )
        return True
    except Exception:
        await release_daily_run(db, "check_overdue_tasks")
        raise


@celery_app.task(name="app.workers.reminder_tasks.check_overdue_tasks", **DAILY_RETRY_KW)
def check_overdue_tasks():
    """记录逾期任务到日志，并推送飞书（webhook 未配置时自动跳过）。"""
    async def _run():
        async with AsyncSessionLocal() as db:
            await _check_overdue_tasks_async(db)

    run_async(_run())


async def _send_checkin_reminder_async(db) -> bool:
    """明日入住提醒核心逻辑。每个 CN 自然日最多推送一次（防 beat 重启刷屏）。"""
    if not await claim_daily_run(db, "send_checkin_reminder"):
        return False

    try:
        tomorrow = today_cn() + timedelta(days=1)
        result = await db.execute(
            select(Order).where(
                Order.check_in_date == tomorrow,
                Order.order_status == OrderStatus.roomed_pending_checkin,
                Order.is_deleted == False,
            )
        )
        orders = result.scalars().all()
        if not orders:
            return False

        logger.info(
            "明日入住提醒 (%s): 共 %d 单 — %s",
            tomorrow,
            len(orders),
            [(o.room_id, o.guest_name) for o in orders],
        )
        lines = [f"{tomorrow}：共 {len(orders)} 单"]
        lines += [f"· {o.room_id or '待排房'} {o.guest_name}" for o in orders]
        await send_checkin_alert(
            "🏨 明日入住提醒",
            lines,
            button_text="去查看",
            button_url=f"{settings.FRONTEND_BASE_URL}/rooms",
        )
        return True
    except Exception:
        await release_daily_run(db, "send_checkin_reminder")
        raise


@celery_app.task(name="app.workers.reminder_tasks.send_checkin_reminder", **DAILY_RETRY_KW)
def send_checkin_reminder():
    """记录明日入住提醒到日志，并推送飞书（webhook 未配置时自动跳过）。"""
    async def _run():
        async with AsyncSessionLocal() as db:
            await _send_checkin_reminder_async(db)

    run_async(_run())


def _checkout_room_label(order: Order) -> str:
    """一张订单在退房提醒里显示的房间号。多房单取全部房号（/ 连接）；
    orders.room_id 已废弃、多房单常为空，故优先取 order.rooms 的房号，回退单房字段。"""
    rids = [r.room_id for r in (order.rooms or []) if r.room_id]
    if not rids and order.room_id:
        rids = [order.room_id]
    return "/".join(rids) if rids else "待排房"


async def _send_checkout_reminder_async(db) -> bool:
    """退房提醒核心逻辑（王总 2026-07-08）：约 11:30 一条消息列出今天该退房但客人
    还没退的房间，前台据此催退房。每个 CN 自然日最多推送一次（防 beat 重启刷屏）。

    「还没退房」判定：check_out_date == 今天 且订单仍是 checked_in（入住中，人没走）。
    一旦退房，订单流转到 pending_checkout、房间转 pending_clean，即从本查询自然排除。
    """
    if not await claim_daily_run(db, "send_checkout_reminder"):
        return False

    try:
        today = today_cn()
        result = await db.execute(
            select(Order)
            .where(
                Order.check_out_date == today,
                Order.order_status == OrderStatus.checked_in,
                Order.is_deleted == False,
            )
            .options(selectinload(Order.rooms))
        )
        orders = result.scalars().all()
        # 续住组中间段：退房日=今天但客人不走（去下一段续住），催退房是误报。
        # 只有组末段（活段口径）退房日到了才是真要走。
        from app.services import stay_group as _stay_group
        kept = []
        for o in orders:
            if o.stay_group_id and not await _stay_group.is_group_last_segment(db, o):
                continue
            kept.append(o)
        orders = kept
        if not orders:
            return False

        logger.info(
            "退房提醒 (%s): 共 %d 单未退房 — %s",
            today,
            len(orders),
            [(o.order_id, o.guest_name) for o in orders],
        )
        lines = [f"今天（{today}）还有 {len(orders)} 间未退房，请提醒客人："]
        lines += [f"· {_checkout_room_label(o)} {o.guest_name}" for o in orders]
        await send_checkout_alert(
            "🚪 退房提醒",
            lines,
            button_text="去查看",
            button_url=f"{settings.FRONTEND_BASE_URL}/rooms",
        )
        return True
    except Exception:
        await release_daily_run(db, "send_checkout_reminder")
        raise


@celery_app.task(name="app.workers.reminder_tasks.send_checkout_reminder", **DAILY_RETRY_KW)
def send_checkout_reminder():
    """约 11:30 推送「哪些房还没退房」到退房提醒群（webhook 未配置时自动跳过）。"""
    async def _run():
        async with AsyncSessionLocal() as db:
            await _send_checkout_reminder_async(db)

    run_async(_run())


async def _send_cleaning_request_card_async(db) -> bool:
    """每日打扫申请卡核心逻辑（PRD 打扫申请群-飞书自助）：列今日续住在住房，
    发一张带【申请打扫】按钮的卡到打扫申请群，保洁自助拿门锁码。每 CN 日最多一次
    （防 beat 重启刷屏）。无续住在住房也发（空态提示），保洁一眼知道今天没活。"""
    if not await claim_daily_run(db, "send_cleaning_request_card"):
        return False
    try:
        from app.services.cleaning_request import (
            list_checkout_rooms_for_cleaning,
            list_instay_rooms_for_cleaning,
            save_daily_card_message_id,
        )
        from app.services.feishu_lead_alert import send_cleaning_request_daily_card

        rooms = await list_instay_rooms_for_cleaning(db)
        checkout_rooms = await list_checkout_rooms_for_cleaning(db)
        logger.info(
            "打扫申请卡 (%s): %d 间续住在住房, %d 间今日退房房",
            today_cn(), len(rooms), len(checkout_rooms),
        )
        message_id = await send_cleaning_request_daily_card(rooms, checkout_rooms)
        # 存下大卡 message_id，供保洁申请后原地更新（标「已申请」）。best-effort。
        if message_id:
            await save_daily_card_message_id(db, message_id)
        return True
    except Exception:
        await release_daily_run(db, "send_cleaning_request_card")
        raise


@celery_app.task(
    name="app.workers.reminder_tasks.send_cleaning_request_card", **DAILY_RETRY_KW
)
def send_cleaning_request_card():
    """每日推送「今日续住房打扫申请」卡到打扫申请群（未配置目标群时优雅降级/跳过）。"""
    async def _run():
        async with AsyncSessionLocal() as db:
            await _send_cleaning_request_card_async(db)

    run_async(_run())


@celery_app.task(name="app.workers.reminder_tasks.run_daily_pricing", **DAILY_RETRY_KW)
def run_daily_pricing():
    """每日北京时间 06:00 为所有房间计算未来30天推荐价格。逐房间提交，失败不影响其他房间。"""

    async def _run():
        from app.models.room import Room
        from app.services.pricing_service import calculate_room_pricing_for_range

        today = today_cn()
        succeeded = 0
        failed = []

        async with AsyncSessionLocal() as session:
            if not await claim_daily_run(session, "run_daily_pricing"):
                logger.info("每日定价今日已执行，跳过 beat 重启补发")
                return

            try:
                rooms_result = await session.execute(select(Room).where(Room.is_deleted == False))
                rooms = rooms_result.scalars().all()
            except Exception:
                # 逐房间的失败在下方各自兜住；这里只兜「一间房都没开始算」的整体失败
                # (如 DB 抖动)，release 后交给 celery autoretry。
                await release_daily_run(session, "run_daily_pricing")
                raise

            for room in rooms:
                if not room.base_price:
                    continue
                try:
                    priced = await calculate_room_pricing_for_range(session, room, today, 30)
                    # Commit per room to avoid batch rollback
                    await session.commit()
                    succeeded += 1
                    logger.info("房间 %s 定价完成: %d 天", room.room_id, priced)
                except Exception as exc:
                    await session.rollback()
                    failed.append(room.room_id)
                    logger.warning("房间 %s 定价失败: %s", room.room_id, exc)

        if failed:
            logger.warning("定价任务: 成功 %d, 失败 %d (%s)", succeeded, len(failed), failed)
        else:
            logger.info("定价任务完成: 成功 %d", succeeded)

    run_async(_run())
