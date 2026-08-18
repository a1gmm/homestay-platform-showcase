"""宝禹同步看门狗 Celery 任务（2026-07-30 拉取停摆 19h 事故的最后一道网）。

检测活在 backend celery——ota-sync 自己报不了「我死了」。逻辑与阈值语义见
services/bypms_pull_health.py 文档串。
"""
import logging
from datetime import datetime, timedelta, timezone

from app.core.database import AsyncSessionLocal
from app.workers.async_helper import run_async
from app.workers.celery_app import celery_app

logger = logging.getLogger(__name__)


async def _bypms_pull_watchdog_async(db=None) -> bool:
    from app.core.config import settings
    from app.services.bypms_pull_health import alert_if_bypms_pull_stale
    from app.services.feishu_lead_alert import send_sync_alert

    async def _run(session) -> bool:
        return await alert_if_bypms_pull_stale(
            session,
            now=datetime.now(timezone.utc),
            threshold=timedelta(minutes=settings.BYPMS_PULL_STALE_ALERT_MINUTES),
            send=send_sync_alert,
            re_alert_interval=timedelta(hours=settings.BYPMS_PULL_REALERT_HOURS),
        )

    if db is not None:
        return await _run(db)
    async with AsyncSessionLocal() as owned:
        return await _run(owned)


@celery_app.task(name="app.workers.sync_watchdog.bypms_pull_watchdog")
def bypms_pull_watchdog():
    """宝禹拉取水位心跳（每 10 分钟）。staging 超 30 分钟无新拉取→同步八成停了，喊人。"""
    return run_async(_bypms_pull_watchdog_async())
