"""Celery tasks that keep order state tidy.

Primary job: auto-cancel orders that are genuinely abandoned — created, never
confirmed, and whose stay window has come and gone without anyone ever assigning
a room or taking a payment. Leaving them around distorts dashboards.

⚠️ 2026-06-05「确认收款挪到退房后」改版后，旧的「pending_confirm + unpaid > 24h = 弃单」
判定彻底失效：新流程下大量**真实未来订单**（OTA 导入 / C 端下单待前台确认）本就长期挂
`pending_confirm + unpaid`。2026-06-17 实测旧逻辑首跑会误删 29 个真实未来订单（含当天入住）。

因此本任务（2026-06-17 重写）只取消**真正的弃单**，须同时满足：
  1. order_status == pending_confirm 且 payment_status == unpaid
  2. 整段入住窗口已过期：所有房（含 Order 自带列 + 每个 OrderRoom）的 check_out_date < today
  3. 从未排房：Order.room_id 与所有 OrderRoom.room_id 均为空
  4. 从未有任何 payment 记录（直查 Payment 表，不信 payment_status 标志）
  5. booking_type == normal（试住/自住单业主出钱，可能长期无付款，不自动取消）
  6. created_at 早于 STALE_ORDER_CUTOFF_HOURS（给前台对刚导入的过期补录单留反应窗口）

任意一条不满足即保留，交人工处理——宁可漏清，不可误删真实预订。
"""
import logging
from datetime import date, datetime, timezone, timedelta

from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.core.datetime_helpers import today_cn
from fastapi import HTTPException

from app.models.order import Order, OrderStatus, PaymentStatus, BookingType
from app.services.order_state import apply_order_transition
from app.models.payment import Payment
from app.services.audit import log_action_tx
from app.workers.async_helper import run_async
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _cancel_stale_pending_orders_async(
    today: date,
    db,
    *,
    now: datetime | None = None,
) -> int:
    """Cancel genuinely-abandoned pending_confirm orders. Returns count cancelled.

    See module docstring for the (deliberately conservative) cancellation criteria.
    `today` / `now` are injectable for testing; production passes today_cn() and utcnow.
    """
    if now is None:
        now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=settings.STALE_ORDER_CUTOFF_HOURS)

    # 先用标量条件圈定候选（状态/付款/软删/单类型/老单 + Order 自带 check_out 已过期），
    # 房间与 payment 记录等关联判定在 Python 层逐单复核（候选集很小，清晰且安全）。
    result = await db.execute(
        select(Order)
        .where(
            Order.order_status == OrderStatus.pending_confirm,
            Order.payment_status == PaymentStatus.unpaid,
            Order.is_deleted.is_(False),
            Order.booking_type == BookingType.normal,
            Order.created_at < cutoff,
            Order.check_out_date < today,
        )
        .options(selectinload(Order.rooms))
    )
    candidates = result.scalars().all()

    cancelled = 0
    for order in candidates:
        # 整段入住窗口已过期：任一子房 check_out >= today 则保留（多房/换房拆分场景）。
        child_checkouts = [r.check_out_date for r in order.rooms if r.check_out_date]
        if any(co >= today for co in child_checkouts):
            continue

        # 从未排房：Order 旧字段 + 所有 OrderRoom 都没有 room_id。
        if order.room_id or any(r.room_id for r in order.rooms):
            continue

        # 从未有任何（未软删的）payment 记录——直查 Payment 表，不信 payment_status 标志
        # （schema 三套真相源，标志可能漂移）。
        pay_count = (await db.execute(
            select(func.count()).select_from(Payment).where(
                Payment.order_id == order.order_id,
                Payment.is_deleted.is_(False),
            )
        )).scalar_one()
        if pay_count:
            continue

        before = {
            "order_status": order.order_status.value,
            "payment_status": order.payment_status.value,
        }
        # 统一走状态机正门（#183 收敛）：守卫（押金等）+ 房态联动都在里面，
        # worker 不再手写 order_status（此前直改会漏掉 reserved 房释放等副作用）。
        try:
            await apply_order_transition(db, order, OrderStatus.cancelled)
        except HTTPException as e:
            logger.warning(
                "auto-cancel skipped %s: %s", order.order_id, e.detail
            )
            continue
        await log_action_tx(
            db,
            operator_id="system",
            action="auto_cancel_stale_order",
            resource_type="order",
            resource_id=order.order_id,
            before_data=before,
            after_data={"order_status": OrderStatus.cancelled.value},
            notes=(
                "auto-cancelled: pending_confirm 弃单，入住窗口已过期、从未排房、"
                f"从未付款、created_at 早于 {settings.STALE_ORDER_CUTOFF_HOURS}h"
            ),
        )
        cancelled += 1

    if cancelled:
        await db.commit()
        logger.info("Auto-cancelled %d stale abandoned orders", cancelled)
    return cancelled


@celery_app.task(name="app.workers.order_maintenance.cancel_stale_pending_orders")
def cancel_stale_pending_orders():
    """Celery entrypoint: open a session and run the abandoned-order sweep.

    Audit log entry is written per cancelled order, attributed to the "system" actor.
    """
    async def _run():
        async with AsyncSessionLocal() as db:
            return await _cancel_stale_pending_orders_async(today_cn(), db)

    return run_async(_run())
