"""Celery entrypoints for monthly-close alerts and incurred-fee self-healing."""

from datetime import date, timedelta
import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import AsyncSessionLocal
from app.core.datetime_helpers import today_cn
from app.workers.async_helper import run_async
from app.workers.celery_app import celery_app


logger = logging.getLogger(__name__)
_SERVICE_FEE_REPAIR_RETRY_KW = dict(
    autoretry_for=(Exception,),
    retry_backoff=60,
    retry_backoff_max=600,
    max_retries=3,
)


def _repair_months(as_of: date) -> list[tuple[int, int]]:
    previous_month_last_day = as_of.replace(day=1) - timedelta(days=1)
    return [
        (previous_month_last_day.year, previous_month_last_day.month),
        (as_of.year, as_of.month),
    ]


async def _repair_incurred_service_fees_async(
    *,
    as_of: date | None = None,
    db: AsyncSession | None = None,
) -> dict:
    """Repair deterministic incurred fees for the current and previous month.

    The current month is always bounded by the Beijing business date, so the
    backstop never turns scheduled future checkouts into estimated expenses.
    Settlement-locked scopes remain report-only. Ambiguous owner-months are
    skipped while unrelated deterministic owner-months are repaired in the
    same run through the shared reconciliation planner.
    """
    if as_of is None:
        as_of = today_cn()
    if db is None:
        async with AsyncSessionLocal() as owned_db:
            return await _repair_incurred_service_fees_async(
                as_of=as_of,
                db=owned_db,
            )

    from scripts.backfill_service_fees import plan_backfill

    months = _repair_months(as_of)
    summary = await plan_backfill(
        db,
        months=months,
        apply=True,
        cutoff=as_of,
        operator_id=None,
        apply_corrections=False,
    )
    safe_summary = {
        "as_of": as_of.isoformat(),
        "months": [f"{year:04d}-{month:02d}" for year, month in months],
        "created": summary["created"],
        "corrected": summary["corrected"],
        "correction_count": summary["correction_count"],
        "created_amount": f"{summary['created_amount']:.2f}",
        "unresolved_count": summary["unresolved_count"],
        "locked_count": summary["locked_count"],
    }
    if (
        summary["unresolved_count"]
        or summary["locked_count"]
        or summary["correction_count"]
    ):
        logger.warning("incurred service-fee repair needs review: %s", safe_summary)
    else:
        logger.info("incurred service-fee repair complete: %s", safe_summary)
    return safe_summary


@celery_app.task(
    name="app.workers.monthly_close_tasks.repair_incurred_service_fees",
    **_SERVICE_FEE_REPAIR_RETRY_KW,
)
def repair_incurred_service_fees() -> dict:
    return run_async(_repair_incurred_service_fees_async())


@celery_app.task(name="app.workers.monthly_close_tasks.scan_monthly_close_alerts")
def scan_monthly_close_alerts() -> None:
    async def _run() -> None:
        from app.services.monthly_close.monitor import scan_monthly_close_alerts as scan

        async with AsyncSessionLocal() as db:
            await scan(db)

    run_async(_run())
