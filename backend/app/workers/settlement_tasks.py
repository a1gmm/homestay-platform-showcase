"""Monthly-close initialization and explicitly invoked settlement generation."""
import logging
from datetime import date, timedelta
from app.core.datetime_helpers import today_cn

from app.workers.celery_app import celery_app
from app.workers.async_helper import run_async
from app.core.database import AsyncSessionLocal
from app.api.v1.settlements import _generate_settlements_core
from app.services.monthly_close.workflow import get_or_create_cycle

logger = logging.getLogger(__name__)


async def initialize_monthly_close_cycle_for_month(db, year: int, month: int):
    """Open the prior-month workflow without writing owner settlement records."""
    billing_month = f"{year:04d}-{month:02d}"
    cycle = await get_or_create_cycle(db, billing_month, None)
    return {
        "cycle_id": cycle.cycle_id,
        "billing_month": cycle.billing_month,
        "status": cycle.status,
    }


async def generate_monthly_settlements_for_month(db, year: int, month: int):
    billing_month = f"{year:04d}-{month:02d}"
    cycle = await get_or_create_cycle(db, billing_month, None)
    observed_control_version = cycle.control_version
    return await _generate_settlements_core(
        db,
        year,
        month,
        created_by=None,
        expected_cycle_id=cycle.cycle_id,
        expected_write_owner="legacy",
        expected_control_version=observed_control_version,
    )


@celery_app.task(name="app.workers.settlement_tasks.initialize_monthly_close_cycle")
def initialize_monthly_close_cycle():
    """每月 1 号开启上月月结；真正结算在核对完成并经管理员确认后生成。"""
    today = today_cn()
    last_month_last_day = today.replace(day=1) - timedelta(days=1)

    async def _run():
        async with AsyncSessionLocal() as db:
            return await initialize_monthly_close_cycle_for_month(
                db,
                last_month_last_day.year,
                last_month_last_day.month,
            )

    try:
        result = run_async(_run())
        logger.info("Monthly close initialized: billing_month=%s", result["billing_month"])
        return result
    except Exception as exc:
        logger.exception("Failed to initialize monthly close: %s", exc)
        raise


@celery_app.task(name="app.workers.settlement_tasks.generate_monthly_settlements")
def generate_monthly_settlements():
    """
    显式调用时生成"上月"的业主结算单。
    已存在的 (owner, billing_month) 会跳过，幂等。
    """
    today = today_cn()
    # 上月的年月
    last_month_last_day = today.replace(day=1) - timedelta(days=1)
    year = last_month_last_day.year
    month = last_month_last_day.month

    async def _run():
        async with AsyncSessionLocal() as db:
            return await generate_monthly_settlements_for_month(db, year, month)

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
