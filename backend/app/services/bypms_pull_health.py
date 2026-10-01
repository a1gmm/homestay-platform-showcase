"""宝禹拉取水位看门狗：ota_raw_orders 长时间无新拉取 → 同步八成停了，按周期告警。

背景（2026-07-30 事故）：宝禹 session 过期后网关回 HTTP 401，逃过 ota-sync 的自动重登，
拉取静默停摆 19 小时。期间 ota-sync 的「连续抓取失败」告警只进系统群（没人看），最后靠
前台上门问才暴露。教训与 7-26 门锁回调静默同款：**检测必须活在被监控者之外**——本模块
跑在 backend celery，不管 ota-sync 是 401、崩溃还是整个进程没跑，只要 staging 水位
（每轮拉取都会刷新全部行的 fetched_at）超阈值不动，就按周期喊人，直到恢复。

401 本身已在 ota-sync 侧根治（raise_for_status_auth_aware → 自动重登）；这里是兜底的
最后一道网。标记存 notification_logs（一行，template=bypms-pull-stale），照抄
lock/callback_health 的节流/复位机制。无 recover：重登需要 Playwright+凭证，只在
ota-sync 侧存在；本看门狗的职责是「让人知道」。
"""
from __future__ import annotations

import inspect
import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.notification import NotificationLog

logger = logging.getLogger(__name__)

_TEMPLATE = "bypms-pull-stale"


def _aware(dt: datetime | None) -> datetime | None:
    if dt is not None and dt.tzinfo is None:   # 落库常为 naive UTC
        return dt.replace(tzinfo=timezone.utc)
    return dt


async def _watermark(db: AsyncSession) -> datetime | None:
    """staging 最近一次拉取时刻。ota_raw_orders 属于 ota-sync（同库无 ORM 模型），裸 SQL 读。
    sqlite（测试库）的裸 max() 返回字符串 → 统一解析。"""
    v = await db.scalar(text(
        "select max(fetched_at) from ota_raw_orders where platform='bypms'"
    ))
    if isinstance(v, str):
        v = datetime.fromisoformat(v)
    return _aware(v)


async def _marker_at(db: AsyncSession) -> datetime | None:
    """最近一次停摆告警时刻（无标记 = 从没告警过 / 已复位）。"""
    return _aware(await db.scalar(
        select(NotificationLog.created_at)
        .where(NotificationLog.template_name == _TEMPLATE)
        .order_by(NotificationLog.created_at.desc())
        .limit(1)
    ))


async def _set_marker(db: AsyncSession, at: datetime) -> None:
    """写「已在 at 时刻告警」标记（始终至多一行）。显式覆盖 created_at 供重报周期判断。"""
    await db.execute(delete(NotificationLog).where(NotificationLog.template_name == _TEMPLATE))
    m = NotificationLog(
        log_id="BP-" + uuid.uuid4().hex[:12].upper(),
        template_name=_TEMPLATE,
        content="STALE",
        status="marker",
    )
    db.add(m)
    await db.flush()
    m.created_at = at
    await db.commit()


async def _clear_marker(db: AsyncSession) -> None:
    await db.execute(delete(NotificationLog).where(NotificationLog.template_name == _TEMPLATE))
    await db.commit()


def _compose(minutes: int) -> str:
    return (
        "⚠️ 宝禹同步可能已停\n"
        f"系统已 {minutes} 分钟没从宝禹拉到数据（正常每 1-2 分钟一轮）。\n"
        "影响：宝禹上的新单/改单/取消不会进系统——查房态/办入住请暂时以宝禹后台为准。\n"
        "处理：请 Cyrus 检查 Railway 的 ota-sync 服务日志；若是登录态问题，"
        "跑 ota-sync/scripts/bypms_login.py 重登即恢复。恢复前本提醒会按周期重发。"
    )


async def alert_if_bypms_pull_stale(
    db: AsyncSession,
    *,
    now: datetime,
    threshold: timedelta,
    send,
    re_alert_interval: timedelta | None = None,
) -> bool:
    """staging 水位距今超 threshold → 告警。返回本轮是否发了告警。

    send: 可同步或异步 callable(text)。
    re_alert_interval: 给了则持续停摆期间按此周期重报（停着一直喊）；None = 只报一次。
    staging 全空（全新环境）→ 拿不到停摆时长实证，宁可不报。
    水位恢复推进 → 自动清标记，日后再停能重新走整套流程。
    """
    last = await _watermark(db)
    if last is None:
        return False

    if now - last <= threshold:
        await _clear_marker(db)
        return False

    marker_at = await _marker_at(db)
    if marker_at is not None:
        not_due = re_alert_interval is None or (now - marker_at) < re_alert_interval
        if not_due:
            return False

    minutes = int((now - last).total_seconds() // 60)
    r = send(_compose(minutes))
    if inspect.isawaitable(r):
        await r
    await _set_marker(db, now)
    logger.warning("宝禹拉取水位告警：停摆 %d 分钟", minutes)
    return True


_SYNC_TEMPLATE = "bypms-sync-health"


async def alert_if_bypms_sync_stale(
    db: AsyncSession, *, now: datetime, threshold: timedelta, send,
    re_alert_interval: timedelta = timedelta(hours=1),
) -> bool:
    """Independent worker checks completed cycles, not only successful source fetches.

    A PostgreSQL transaction lock prevents concurrent workers from duplicating alerts.
    Only acknowledged delivery changes the durable incident marker. No order writes.
    """
    if db.bind.dialect.name == "postgresql":
        await db.execute(text("SET LOCAL statement_timeout = '10s'"))
        await db.execute(text("SET LOCAL lock_timeout = '2s'"))
        if not await db.scalar(text("SELECT pg_try_advisory_xact_lock(260926731)")):
            return False
    row = (await db.execute(text(
        "SELECT max(finished_at) AS completed FROM bypms_sync_cycles WHERE status='succeeded'"
    ))).first()
    completed = row[0] if row else None
    if isinstance(completed, str):
        completed = datetime.fromisoformat(completed)
    completed = _aware(completed)
    pull = await _watermark(db)
    recent = lambda at: at is not None and -60 <= (now - at).total_seconds() <= threshold.total_seconds()
    healthy = recent(completed) and recent(pull)
    marker = (await db.execute(select(NotificationLog).where(
        NotificationLog.template_name == _SYNC_TEMPLATE,
    ).order_by(NotificationLog.created_at.desc()).limit(1))).scalars().first()
    if healthy and marker is None:
        return False
    if not healthy and marker and now - _aware(marker.created_at) < re_alert_interval:
        return False
    def stamp(at):
        return at.astimezone(timezone(timedelta(hours=8))).strftime('%m-%d %H:%M:%S') if at else '没有可确认记录'
    if healthy:
        body = ("宝禹同步已恢复\n"
                f"最近完整成功：{stamp(completed)}（北京时间）。\n"
                f"最近订单抓取：{stamp(pull)}。\n"
                "完整同步已重新完成；订单锁定和业务差异仍需在同步管理页核对，不代表每笔差异都已解决。")
    else:
        body = ("宝禹同步需要检查\n"
                f"最近完整成功：{stamp(completed)}；最近订单抓取：{stamp(pull)}（北京时间）。\n"
                f"至少一项超过 {int(threshold.total_seconds() // 60)} 分钟或时间异常。\n"
                "抓取正常也可能存在后续处理失败，请检查同步管理页的周期和失败步骤；核对前以宝禹后台为准。")
    delivered = send(body)
    if inspect.isawaitable(delivered):
        delivered = await delivered
    if delivered is not True:
        logger.warning("BYPMS health notification not acknowledged; retain incident for retry")
        return False
    await db.execute(delete(NotificationLog).where(NotificationLog.template_name == _SYNC_TEMPLATE))
    if not healthy:
        db.add(NotificationLog(log_id="BH-" + uuid.uuid4().hex[:12].upper(),
            template_name=_SYNC_TEMPLATE, content="STALE", status="marker", created_at=now))
    await db.commit()
    return True
