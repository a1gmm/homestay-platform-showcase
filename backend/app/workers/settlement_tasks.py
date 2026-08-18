"""
Celery task to auto-generate monthly owner settlements on the 1st of each month.
"""
import logging
from datetime import date, timedelta
from app.core.datetime_helpers import today_cn

from app.workers.celery_app import celery_app
from app.workers.async_helper import run_async
from app.core.database import AsyncSessionLocal
from app.api.v1.settlements import _generate_settlements_core

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.settlement_tasks.generate_monthly_settlements")
def generate_monthly_settlements():
    """
    每月 1 号跑：生成"上月"的业主结算单。
    已存在的 (owner, billing_month) 会跳过，幂等。
    """
    today = today_cn()
    # 上月的年月
    last_month_last_day = today.replace(day=1) - timedelta(days=1)
    year = last_month_last_day.year
    month = last_month_last_day.month

    async def _run():
        async with AsyncSessionLocal() as db:
            return await _generate_settlements_core(db, year, month, created_by=None)

    try:
        result = run_async(_run())
        logger.info(
            "Monthly settlements generated: %s owners, billing_month=%s",
            result.get("generated"), result.get("billing_month"),
        )
        return result
    except Exception as e:
        logger.exception("Failed to generate monthly settlements: %s", e)
        raise
