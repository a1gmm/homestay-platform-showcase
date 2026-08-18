"""Housekeeping Celery tasks.

Daily job: find rooms that have been sitting idle (no non-cancelled orders
checking out in the last ``IDLE_MAINTENANCE_DAYS`` days) and generate a
"maintenance reminder" task so a keeper (管家) can pick it up from the staff
portal and schedule routine upkeep (ventilation, dusting, dehumidification).

The reminder is a ``TaskType.custom`` row with a ``[维护提醒]`` title prefix —
that prefix is also the dedupe key, so re-running the job does not pile
duplicate reminders onto the same room.
"""
import logging
import uuid
from datetime import date, datetime, timedelta, timezone
from app.core.datetime_helpers import today_cn

from sqlalchemy import Date, case, func, select, type_coerce, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import AsyncSessionLocal
from app.models.order import Order, OrderStatus
from app.models.room import Room, RoomStatus
from app.models.task import Task, TaskPriority, TaskStatus, TaskType
from app.services.audit import log_action_tx
from app.workers.async_helper import run_async
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)

_REMINDER_TITLE_PREFIX = "[维护提醒]"

_CLEANING_STATUSES = (RoomStatus.pending_clean, RoomStatus.cleaning)


def plan_stale_pending_clean_clears(rows, now: datetime, threshold_hours: int) -> list[str]:
    """纯函数：从 (room_id, room_status, updated_at) 里挑出「卡住太久」的待清扫/清扫中房。

    正常保洁退房当天即点「打扫完了」把房态复位；房态在 pending_clean/cleaning
    停留超过 ``threshold_hours`` 小时，必是残留（收尾漏点 / 退房后取消订单未回滚 /
    引擎绕过通知没发卡）。用「静态房态停留时长」而非「有无未完成任务」判定——
    因为一间房可能堆着多张历史清扫任务（只关掉最新一张），有残留任务≠还在扫。

    Args:
        rows: 可迭代的 (room_id, room_status, updated_at)。room_status 可是
            ``RoomStatus`` 枚举或其字符串值；updated_at 为 tz-aware 或 naive(按 UTC)。
        now: 参照当前时间（tz-aware UTC）。
        threshold_hours: 判定阈值小时数。
    """
    cutoff = now - timedelta(hours=threshold_hours)
    stale: list[str] = []
    for room_id, status, updated_at in rows:
        status_val = getattr(status, "value", status)
        if status_val not in ("pending_clean", "cleaning"):
            continue
        if updated_at is None:
            continue
        if updated_at.tzinfo is None:
            updated_at = updated_at.replace(tzinfo=timezone.utc)
        if updated_at <= cutoff:
            stale.append(room_id)
    return stale


@celery_app.task(name="app.workers.housekeeping_tasks.reconcile_stale_pending_clean")
def reconcile_stale_pending_clean():
    """自动清理「卡住的待清扫」：房态卡在 pending_clean/cleaning 超阈值即清回 available。

    返回 ``{enabled, threshold_hours, scanned, cleared, room_ids}`` 便于日志排查。
    """
    return run_async(_reconcile_stale_pending_clean_async())


async def _reconcile_stale_pending_clean_async(
    now: datetime | None = None,
    threshold_hours: int | None = None,
    db: AsyncSession | None = None,
) -> dict:
    """扫全量 pending_clean/cleaning 房，把卡住太久的清回 available 并作废残留清扫任务。

    幂等：只动 room_status 仍为 pending_clean/cleaning 的房；清回 available 后审计留痕，
    并把该房 pending/in_progress 的清扫任务置 cancelled（防日后误点旧卡触发房东计费）。
    受 ``STALE_PENDING_CLEAN_RECON_ENABLED`` kill switch 控制，关时直接跳过。
    """
    if db is None:
        async with AsyncSessionLocal() as owned_db:
            return await _reconcile_stale_pending_clean_async(
                now=now, threshold_hours=threshold_hours, db=owned_db,
            )

    if not settings.STALE_PENDING_CLEAN_RECON_ENABLED:
        return {"enabled": False, "cleared": 0, "room_ids": []}

    if now is None:
        now = datetime.now(timezone.utc)
    threshold = threshold_hours if threshold_hours is not None else settings.STALE_PENDING_CLEAN_HOURS

    rows = (
        await db.execute(
            select(Room.room_id, Room.room_status, Room.updated_at)
            .where(Room.room_status.in_(_CLEANING_STATUSES))
            .order_by(Room.room_id)
        )
    ).all()
    scanned = len(rows)
    stale_ids = plan_stale_pending_clean_clears(
        ((r.room_id, r.room_status, r.updated_at) for r in rows), now, threshold,
    )

    if not stale_ids:
        logger.info("Stale-pending-clean scan: scanned=%d cleared=0", scanned)
        return {"enabled": True, "threshold_hours": threshold, "scanned": scanned,
                "cleared": 0, "room_ids": []}

    # 房态清回 available（守卫：只动仍是 pending_clean/cleaning 的行，防并发）。
    upd_rooms = await db.execute(
        update(Room)
        .where(Room.room_id.in_(stale_ids), Room.room_status.in_(_CLEANING_STATUSES))
        .values(room_status=RoomStatus.available, updated_at=func.now())
    )
    cleared = upd_rooms.rowcount or 0
    # 作废残留清扫任务，避免日后误点旧卡触发房东计费/再次漂移。
    await db.execute(
        update(Task)
        .where(
            Task.room_id.in_(stale_ids),
            Task.task_type == TaskType.cleaning,
            Task.status.in_((TaskStatus.pending, TaskStatus.in_progress)),
        )
        .values(status=TaskStatus.cancelled)
    )
    await log_action_tx(
        db,
        operator_id="system",
        action="auto_clear_stale_pending_clean",
        resource_type="housekeeping",
        resource_id=None,
        after_data={"threshold_hours": threshold, "cleared": cleared, "room_ids": stale_ids},
        notes=f"自动清理卡住>{threshold}h 的待清扫房 {cleared} 间：{stale_ids}",
    )
    await db.commit()

    # best-effort 飞书告警（前台可抽查），失败不阻断。
    try:
        from app.services.feishu_lead_alert import send_ops_alert
        await send_ops_alert(
            f"🧹 房态自动纠错：清理了 {cleared} 间卡住>{threshold}h 的「待清扫」房"
            f"（已回空置）：{', '.join(stale_ids)}"
        )
    except Exception:
        logger.exception("[stale-pending-clean] 告警发送失败（不影响清理）")

    summary = {"enabled": True, "threshold_hours": threshold, "scanned": scanned,
               "cleared": cleared, "room_ids": stale_ids}
    logger.info("Stale-pending-clean scan complete: %s", summary)
    return summary


@celery_app.task(name="app.workers.housekeeping_tasks.detect_idle_rooms")
def detect_idle_rooms():
    """Detect idle available rooms and enqueue keeper maintenance reminders.

    Returns a summary dict ``{created, skipped, threshold_days, basis_date}``
    so Flower / Celery logs capture a quick readout per run.
    """
    return run_async(_detect_idle_rooms_async())


async def _detect_idle_rooms_async(
    basis_date: date | None = None,
    db: AsyncSession | None = None,
) -> dict:
    """Scan for idle rooms and enqueue maintenance reminders.

    Args:
        basis_date: the date to measure "idle since" against. Defaults to
            ``today_cn()`` (Asia/Shanghai 当天)。进程跑在 UTC 时 ``date.today()``
            会差一天,故显式用北京时间 (#47)。Parameterised mainly so tests can
            pin a deterministic reference.
        db: optional session. When omitted, a fresh ``AsyncSessionLocal`` is
            opened and closed automatically. Tests pass an explicit session
            backed by the in-memory SQLite fixture.
    """
    if db is None:
        async with AsyncSessionLocal() as owned_db:
            return await _detect_idle_rooms_async(basis_date=basis_date, db=owned_db)

    threshold = settings.IDLE_MAINTENANCE_DAYS
    if basis_date is None:
        basis_date = today_cn()
    cutoff_date = basis_date - timedelta(days=threshold)

    # last_activity = MAX non-cancelled check_out_date for this room,
    # falling back to DATE(room.created_at) if no order exists.
    # ``func.date(...)`` works on both PostgreSQL and SQLite and avoids the
    # SQLite CAST→datetime quirk that breaks SQLAlchemy's Date result processor.
    last_checkout_expr = func.max(
        case(
            (Order.order_status != OrderStatus.cancelled, Order.check_out_date),
            else_=None,
        )
    )
    last_activity_expr = type_coerce(
        func.coalesce(last_checkout_expr, func.date(Room.created_at)),
        Date,
    )

    stmt = (
        select(
            Room.room_id,
            Room.room_name,
            last_activity_expr.label("last_activity_date"),
        )
        .outerjoin(Order, Order.room_id == Room.room_id)
        .where(Room.room_status == RoomStatus.available)
        .group_by(Room.room_id, Room.room_name, Room.created_at)
        .having(last_activity_expr <= cutoff_date)
        .order_by(Room.room_id)
    )
    idle_rooms = (await db.execute(stmt)).all()
    logger.info(
        "Idle-room scan: basis_date=%s threshold=%d candidates=%d",
        basis_date, threshold, len(idle_rooms),
    )

    created = 0
    skipped = 0

    for row in idle_rooms:
        room_id: str = row.room_id
        last_activity = row.last_activity_date
        # SQLite may return ISO strings for date columns; normalise.
        if isinstance(last_activity, str):
            last_activity = date.fromisoformat(last_activity[:10])
        days_idle = (basis_date - last_activity).days

        # Dedupe: if this room already has an open reminder, skip.
        existing = await db.execute(
            select(Task.task_id)
            .where(
                Task.room_id == room_id,
                Task.task_type == TaskType.custom,
                Task.status.in_((TaskStatus.pending, TaskStatus.in_progress)),
                Task.title.like(f"{_REMINDER_TITLE_PREFIX}%"),
            )
            .limit(1)
        )
        if existing.first() is not None:
            skipped += 1
            continue

        task = Task(
            task_id="TSK-" + uuid.uuid4().hex[:12].upper(),
            task_type=TaskType.custom,
            title=f"{_REMINDER_TITLE_PREFIX} 房号 {room_id} 已闲置 {days_idle} 天",
            description=(
                f"连续 {days_idle} 天未使用，请安排通风、清洁、除湿等日常维护，"
                "完成后标记任务为已完成。"
            ),
            room_id=room_id,
            priority=TaskPriority.medium,
            deadline=datetime.now(timezone.utc) + timedelta(days=2),
            created_by=None,  # system
        )
        db.add(task)
        created += 1

    if created:
        await log_action_tx(
            db,
            operator_id="system",
            action="auto_generate_idle_maintenance_tasks",
            resource_type="housekeeping",
            resource_id=None,
            after_data={
                "created": created,
                "skipped": skipped,
                "threshold_days": threshold,
                "basis_date": basis_date.isoformat(),
            },
            notes=(
                f"basis={basis_date} threshold={threshold}d "
                f"created={created} skipped={skipped}"
            ),
        )
        await db.commit()

    summary = {
        "created": created,
        "skipped": skipped,
        "threshold_days": threshold,
        "basis_date": basis_date.isoformat(),
    }
    logger.info("Idle-room scan complete: %s", summary)
    return summary


# ─── 续住关联组自愈（eng-review #3）─────────────────────────────────────────────
# bypms 同步引擎在独立仓、直接改库退订我们的单，不会调 app 侧 unlink。此任务是
# catch-all 兜底：定时把续住组里已取消/已删除的成员解关联，避免组挂着退订孤儿单
# 误导门锁/看板/报表。unlink 本身幂等且低风险；gate STAY_GROUP_SELFHEAL_LIVE=0 可关。
import os


async def _selfheal_stay_groups_async(db: AsyncSession | None = None) -> int:
    from app.services.stay_group import selfheal_stay_groups
    if db is not None:
        n = await selfheal_stay_groups(db)
        await db.commit()
        return n
    async with AsyncSessionLocal() as owned:
        n = await selfheal_stay_groups(owned)
        await owned.commit()
        logger.info("stay-group selfheal: unlinked %s dead member(s)", n)
        return n


@celery_app.task(name="app.workers.housekeeping_tasks.selfheal_stay_groups")
def selfheal_stay_groups_task():
    if os.getenv("STAY_GROUP_SELFHEAL_LIVE", "1") != "1":
        logger.info("stay-group selfheal skipped (STAY_GROUP_SELFHEAL_LIVE!=1)")
        return {"skipped": True}
    return {"unlinked": run_async(_selfheal_stay_groups_async())}


# ─── 单房单房费镜像自愈（2026-07-15）──────────────────────────────────────────
# 对账用裸 SQL 脚本改 orders.actual_price 却没同步 order_rooms.actual_price → 业主门户/
# 结算按每房价聚合读到旧值、房费虚低。此任务把单房单的每房价拉回订单级真值（单房单二者
# 恒等）。幂等、只碰单房单。gate SINGLE_ROOM_PRICE_SELFHEAL_LIVE=0 可关。
async def _selfheal_single_room_prices_async(db: AsyncSession | None = None) -> int:
    from app.services.order_price_selfheal import selfheal_single_room_prices
    if db is not None:
        n = await selfheal_single_room_prices(db)
        await db.commit()
        return n
    async with AsyncSessionLocal() as owned:
        n = await selfheal_single_room_prices(owned)
        await owned.commit()
        logger.info("single-room price selfheal: mirrored %s room(s) to order total", n)
        return n


@celery_app.task(name="app.workers.housekeeping_tasks.selfheal_single_room_prices")
def selfheal_single_room_prices_task():
    if os.getenv("SINGLE_ROOM_PRICE_SELFHEAL_LIVE", "1") != "1":
        logger.info("single-room price selfheal skipped (SINGLE_ROOM_PRICE_SELFHEAL_LIVE!=1)")
        return {"skipped": True}
    return {"mirrored": run_async(_selfheal_single_room_prices_async())}
