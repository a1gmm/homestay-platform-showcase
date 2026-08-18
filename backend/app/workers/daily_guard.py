"""Per-day idempotency guard for daily Celery beat tasks.

Why this exists
---------------
celery beat runs embedded (``-B``) inside the single celery-worker on Railway,
whose container filesystem is ephemeral. Beat's ``celerybeat-schedule`` state
file (which records each task's last run time) is wiped on every redeploy, so on
startup beat treats every "daily" crontab task as never-run and fires it
immediately. Because the worker autodeploys on every push to ``main``, the daily
Feishu reminders were sent once per deploy — spamming the ops chat regardless of
the configured cron hours. Lowering the cron frequency did nothing because the
spam comes from the restart-fire, not the schedule.

This guard records a per-(task, CN-date) marker row and makes each daily task a
no-op once it has run for the current calendar day, so each fires at most once
per day no matter how many times beat restarts.

The marker is stored in ``notification_logs`` (no new migration needed). The CN
date is written explicitly into ``content`` so the dedup key never drifts with
the UTC/CN day boundary. Beat is single-instance, so a plain check-then-insert is
race-free in practice.
"""
import logging
import uuid

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.datetime_helpers import today_cn
from app.models.notification import NotificationLog

logger = logging.getLogger(__name__)

_MARKER_PREFIX = "daily-guard:"


async def claim_daily_run(db: AsyncSession, task_key: str) -> bool:
    """Claim today's run slot for ``task_key``.

    Returns ``True`` if this is the first claim for the current CN day (the
    caller should proceed) and ``False`` if today's slot was already taken (the
    caller should skip — this is a beat restart re-fire).
    """
    today_str = today_cn().isoformat()
    template = _MARKER_PREFIX + task_key

    existing = await db.scalar(
        select(NotificationLog.log_id)
        .where(
            NotificationLog.template_name == template,
            NotificationLog.content == today_str,
        )
        .limit(1)
    )
    if existing:
        logger.info("daily task %s 今日已执行 (%s)，跳过 beat 重启补发", task_key, today_str)
        return False

    db.add(
        NotificationLog(
            log_id="DG-" + uuid.uuid4().hex[:12].upper(),
            template_name=template,
            content=today_str,
            status="marker",
        )
    )
    await db.commit()
    return True


async def release_daily_run(db: AsyncSession, task_key: str) -> None:
    """Release today's run slot after the task body failed.

    claim 在任务开头就 commit 了 marker；若之后任务主体抛异常(飞书超时等)，
    不释放的话当天既不会被 celery 重试成功、也不会被 beat 重启补发——提醒静默
    丢失一整天。调用方在 except 里 release 后必须把异常继续抛出，交给 celery
    autoretry。release 自身失败(DB 不可用)时 marker 保留，宁可少发不重复刷屏。
    """
    await db.rollback()  # 任务主体异常后 session 可能处于中断态，先复位
    today_str = today_cn().isoformat()
    template = _MARKER_PREFIX + task_key
    await db.execute(
        delete(NotificationLog).where(
            NotificationLog.template_name == template,
            NotificationLog.content == today_str,
        )
    )
    await db.commit()
    logger.warning("daily task %s 失败，已释放今日 slot (%s) 等待重试", task_key, today_str)
