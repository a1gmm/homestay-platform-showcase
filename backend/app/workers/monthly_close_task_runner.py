"""Broker notifications accelerate a durable database queue; beat recovers loss."""
from app.core.database import AsyncSessionLocal
from app.workers.async_helper import run_async
from app.workers.celery_app import celery_app


@celery_app.task(name="app.workers.monthly_close_task_runner.run_monthly_close_task", acks_late=True, reject_on_worker_lost=True, soft_time_limit=150, time_limit=170)
def run_monthly_close_task(task_id: str):
    from app.services.monthly_close.task_planner import advance_task
    from app.services.monthly_close.task_runtime import execute_task
    return run_async(execute_task(task_id, advance_task))


@celery_app.task(name="app.workers.monthly_close_task_runner.poll_monthly_close_tasks", ignore_result=True)
def poll_monthly_close_tasks():
    from app.services.monthly_close.task_runtime import pending_task_ids

    async def scan():
        async with AsyncSessionLocal() as db:
            return await pending_task_ids(db)

    task_ids = run_async(scan())
    dispatched = 0
    for task_id in task_ids:
        # A failed publish leaves the row queued; the next beat retries it.
        run_monthly_close_task.delay(task_id)
        dispatched += 1
    return {"dispatched": dispatched}
