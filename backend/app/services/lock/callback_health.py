"""回调心跳 + 自愈闭环：lock_events 长时间无新行 → 慧享家回调链路可能已断。

慧享家 lockLogPush 回调是我方唯一的下码异步确认（logType=8）+ 电量/在线自愈来源。
地址/Token 失效时厂商不报错、闷着不推（2026-07-12 断了 9 天；2026-07-21 又断，仅
7-23 报了一条飞书没人看见、静默 5 天）。教训有二，本模块各补一道：

1. **报一次不够**：老逻辑「静默期只报一次」→ 那一条被漏看就再无提醒。传 re_alert_interval
   后按周期**重报**，断着就一直喊，直到恢复。
2. **检测与自愈脱节**：检测在每小时心跳、重登记在每天 4:20，两者不通气，断了要等到次日
   凌晨才尝试自愈、且成败无人知。改为检测到静默即调 recover()**当场尝试重登记**，并把
   成败写进告警——闭环。

恢复（又来新事件）后自动复位标记，日后再断能重新走整套流程。标记存 notification_logs
（一行，template=lock-callback-silent），与历史生产标记兼容。
"""
from __future__ import annotations

import inspect
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.lock_event import LockEvent
from app.models.notification import NotificationLog

logger = logging.getLogger(__name__)

_TEMPLATE = "lock-callback-silent"
_SENTINEL = "SILENT"   # 标记行内容（与历史生产标记兼容）


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:   # server_default now() 落库常为 naive UTC
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _marker_at(db: AsyncSession) -> datetime | None:
    """最近一次静默告警时刻（无标记 = 从没告警过 / 已复位）。"""
    return _aware(await db.scalar(
        select(NotificationLog.created_at)
        .where(NotificationLog.template_name == _TEMPLATE)
        .order_by(NotificationLog.created_at.desc())
        .limit(1)
    ))


async def _set_marker(db: AsyncSession, at: datetime) -> None:
    """写「已在 at 时刻告警」标记（始终至多一行）。显式写 created_at=at 供重报周期判断
    （server_default 会填真实 now，这里要的是传入的逻辑时刻，与测试 _add_event 同手法）。"""
    await db.execute(delete(NotificationLog).where(NotificationLog.template_name == _TEMPLATE))
    m = NotificationLog(
        log_id="CB-" + uuid.uuid4().hex[:12].upper(),
        template_name=_TEMPLATE,
        content=_SENTINEL,
        status="marker",
    )
    db.add(m)
    await db.flush()
    m.created_at = at
    await db.commit()


async def _clear_marker(db: AsyncSession) -> None:
    await db.execute(delete(NotificationLog).where(NotificationLog.template_name == _TEMPLATE))
    await db.commit()


def _compose(hours: int, recovered: bool | None) -> str:
    head = (
        "⚠️ 门锁回调可能已断\n"
        f"已 {hours} 小时没收到任何门锁事件（lockLogPush）。\n"
        "影响：新密码下发无法确认、电量/在线状态停更。\n"
    )
    if recovered is True:
        tail = ("已自动重新登记回调地址（本轮成功）。若下一轮仍无事件，"
                "说明厂商侧未生效，请人工检查登记与 Token。")
    elif recovered is False:
        tail = ("自动重新登记回调地址【失败】！请立即人工用 register_lock_webhook 脚本重登，"
                "并检查回调地址/Token 是否有效。")
    else:
        tail = "请检查回调地址/Token 是否有效，并用 register_lock_webhook 脚本重新登记。"
    return head + tail


async def alert_if_callback_silent(
    db: AsyncSession,
    *,
    now: datetime,
    threshold: timedelta,
    send,
    recover=None,
    re_alert_interval: timedelta | None = None,
) -> bool:
    """lock_events 最近一行距今超 threshold → 尝试自愈 + 告警。返回本轮是否发了告警。

    send: 可同步或异步的 callable(text)（生产传 async send_lock_alert，测试传 lambda）。
    recover: 可选 async/sync callable()->bool，检测到静默时当场尝试自愈重登记回调地址，
             成败写进告警。**每轮静默都会调用**（幂等、便宜，尽快救回），与告警节流解耦。
    re_alert_interval: 给了则持续静默期间按此周期**重报**（断着一直喊）；None = 老行为，
             只报一次。
    从没收到过任何事件（last=None）→ 拿不到静默时长实证，宁可不报（避免全新部署误报）。
    """
    last = _aware(await db.scalar(select(func.max(LockEvent.created_at))))
    if last is None:
        return False

    if now - last <= threshold:
        await _clear_marker(db)   # 回调健康：复位，日后再断能重新告警
        return False

    # —— 静默超阈值 —— #
    # 1) 每轮都尝试自愈重登记（幂等，尽快救回，不等每天 4:20）
    recovered: bool | None = None
    if recover is not None:
        try:
            r = recover()
            recovered = (await r) if inspect.isawaitable(r) else r
        except Exception:
            logger.exception("回调静默自愈重登记抛异常")
            recovered = False

    # 2) 告警按周期节流：首次静默必报；之后每 re_alert_interval 重报一次
    marker_at = await _marker_at(db)
    if marker_at is not None:
        not_due = re_alert_interval is None or (now - marker_at) < re_alert_interval
        if not_due:
            return False

    hours = int((now - last).total_seconds() // 3600)
    text = _compose(hours, recovered)
    r = send(text)
    if inspect.isawaitable(r):
        await r
    await _set_marker(db, now)
    logger.warning("门锁回调心跳告警：静默 %d 小时；自愈重登记=%s", hours, recovered)
    return True
