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


def _monthly_close_automation_allowed(action) -> bool:
    """Re-check both the feature boundary and the non-user worker identity."""
    from app.services.monthly_close.permissions import (
        MonthlyCloseFeature,
        MonthlyClosePermissionDenied,
        MonthlyCloseSystemActor,
        assert_monthly_close_feature_enabled,
        assert_monthly_close_system_action_allowed,
    )

    try:
        assert_monthly_close_feature_enabled(MonthlyCloseFeature.low_risk_automation)
        assert_monthly_close_system_action_allowed(
            MonthlyCloseSystemActor(
                actor_id=(
                    "monthly_close_monitor"
                    if action.value in {"scan", "notify"}
                    else "monthly_close_processor"
                ),
                verified=True,
            ),
            action,
        )
    except MonthlyClosePermissionDenied as exc:
        logger.warning("monthly-close automation skipped: %s", exc.code)
        return False
    return True


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
    from app.services.monthly_close.permissions import MonthlyCloseSystemAction

    if not _monthly_close_automation_allowed(
        MonthlyCloseSystemAction.scan
    ) or not _monthly_close_automation_allowed(MonthlyCloseSystemAction.notify):
        return

    async def _run() -> None:
        from app.services.monthly_close.monitor import scan_monthly_close_alerts as scan

        async with AsyncSessionLocal() as db:
            await scan(db)

    run_async(_run())


@celery_app.task(name="app.workers.monthly_close_tasks.process_monthly_close_job")
def process_monthly_close_job(job_id: str) -> dict[str, str]:
    """Resolve durable processing state from a broker-delivered job reference."""
    from app.services.monthly_close.permissions import MonthlyCloseSystemAction

    if not _monthly_close_automation_allowed(
        MonthlyCloseSystemAction.classify_safe
    ):
        return {"job_id": job_id, "processing_status": "automation_disabled"}
    from app.services.monthly_close import processing

    return run_async(processing.process_monthly_close_job_async(job_id))


@celery_app.task(name="app.workers.monthly_close_tasks.dispatch_monthly_close_outbox", ignore_result=True)
def dispatch_monthly_close_outbox(batch_size: int = 50) -> dict[str, int]:
    """Publish pending durable job references; failures remain in the outbox."""
    from app.services.monthly_close.permissions import MonthlyCloseSystemAction

    if not _monthly_close_automation_allowed(
        MonthlyCloseSystemAction.classify_safe
    ):
        return {"published": 0, "failed": 0, "skipped_disabled": 1}
    from app.services.monthly_close.processing import dispatch_processing_outbox

    return run_async(dispatch_processing_outbox(batch_size=batch_size))


@celery_app.task(
    name="app.workers.monthly_close_tasks.reconcile_stale_assistant_runs"
)
def reconcile_stale_assistant_runs() -> dict[str, int]:
    """Converge assistant runs orphaned by worker/process termination."""
    from app.services.monthly_close.permissions import MonthlyCloseSystemAction

    if not _monthly_close_automation_allowed(
        MonthlyCloseSystemAction.classify_safe
    ):
        return {"recovered": 0, "skipped_disabled": 1}

    async def _run() -> dict[str, int]:
        from app.services.monthly_close.assistant import (
            reconcile_stale_assistant_runs as reconcile,
        )

        async with AsyncSessionLocal() as db:
            recovered = await reconcile(db)
            return {"recovered": recovered}

    return run_async(_run())
