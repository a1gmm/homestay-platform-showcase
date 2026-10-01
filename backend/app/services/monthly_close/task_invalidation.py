"""Invalidate task evidence in the same transaction as business facts.

The explicit allowlist covers API, worker and ORM service writes. Cycle-bound
facts are scoped exactly. Cross-month business records (orders, room ownership,
expense dates and their old values) conservatively invalidate all existing tasks.
Paused/failed tasks remain under user control. Task, proposal, run and audit rows
are deliberately excluded, so preparing a proposal cannot create a wake loop.

This hook covers ORM flushes and SQLAlchemy session.execute DML. Raw SQL scripts
must call mark_dirty explicitly; their SQL text is never guessed or parsed here.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

from sqlalchemy import event, inspect, or_, select, update
from sqlalchemy.orm import Session, attributes

from app.models.monthly_close import MonthlyCloseCycle
from app.models.monthly_close_task import MonthlyCloseTask

EVIDENCE_TABLES = frozenset({
    "orders", "order_rooms", "order_source_price_snapshots", "order_sync_conflicts",
    "managed_stay_groups", "company_sponsored_stays", "company_sponsorship_adjustments",
    "payments", "refunds", "expenses", "service_fee_config", "room_cost_share_rules",
    "cleaning_requests", "cleaning_work_imports", "cleaning_work_records",
    "cleaning_work_resolutions", "cleaning_work_removals",
    "owner_settlements", "owner_settlement_items", "rooms", "owners",
    "recon_batches", "recon_diffs", "utility_recon_uploads", "utility_recon_batches",
    "utility_recon_rows", "utility_recon_suggestions",
    "monthly_close_cycles", "monthly_close_documents", "monthly_close_inbox_items",
    "monthly_close_document_analyses", "monthly_close_source_requirements",
    "monthly_close_step_confirmations", "monthly_close_service_lines",
    "monthly_close_processing_jobs", "monthly_close_ota_settlement_consumptions",
    "financial_case_sources",
})
_WAKE_STATUSES = ("queued", "running", "waiting_user", "waiting_approval", "succeeded")
_IGNORED_COLUMNS = {"created_at", "updated_at"}
_NEW_TASKS_KEY = "monthly_close_new_task_ids"
_SYNC_KEY = "monthly_close_invalidated_task_values"
_TABLE_PRESENT_KEY = "monthly_close_task_table_present"


def _changed_columns(obj):
    state = inspect(obj)
    return {column.key for column in state.mapper.column_attrs if state.attrs[column.key].history.has_changes()} - _IGNORED_COLUMNS


def _scope_values(obj, key):
    state = inspect(obj)
    if key not in state.attrs:
        return set()
    history = state.attrs[key].history
    # __dict__ reads avoid accidental lazy queries inside a flush callback.
    return {value for value in [obj.__dict__.get(key), *history.deleted, *history.added] if value}


def _invalidate(session, *, cycle_ids=None, months=None, all_cycles=False):
    table = MonthlyCloseTask.__table__
    statement = select(table).where(table.c.status.in_(_WAKE_STATUSES))
    excluded = session.info.get(_NEW_TASKS_KEY, set())
    if excluded:
        statement = statement.where(table.c.task_id.not_in(excluded))
    if not all_cycles:
        scopes = []
        if cycle_ids:
            scopes.append(table.c.cycle_id.in_(cycle_ids))
        if months:
            scopes.append(table.c.cycle_id.in_(select(MonthlyCloseCycle.cycle_id).where(MonthlyCloseCycle.billing_month.in_(months))))
        if not scopes:
            return
        statement = statement.where(or_(*scopes))
    connection = session.connection()
    # During a rolling rollback, ordinary business tables predate durable tasks.
    # Inspect before issuing task SQL: PostgreSQL cannot recover an undefined-table
    # statement without rolling back the caller's business transaction.
    # Cache only within this transaction so an upgrade becomes visible next time.
    if _TABLE_PRESENT_KEY not in session.info:
        session.info[_TABLE_PRESENT_KEY] = inspect(connection).has_table(table.name)
    if not session.info[_TABLE_PRESENT_KEY]:
        return
    rows = connection.execute(statement.with_for_update()).mappings().all()
    pending_sync = session.info.setdefault(_SYNC_KEY, {})
    for row in rows:
        if row["status"] == "queued" and (row["checkpoint"] or {}).get("evidence_dirty") and not row["lease_token"]:
            continue
        now = datetime.now(timezone.utc)
        checkpoint = {**copy.deepcopy(row["checkpoint"] or {}), "continuous_steps": 0, "evidence_dirty": True, "delivery_verified": False}
        result = copy.deepcopy(row["result"] or {})
        if row["status"] == "succeeded":
            result.update(summary="资料已更新，正在重新核验交付。", artifacts=[], evidence_hash="")
        events = [*(row["events"] or []), {"kind": "business_evidence_changed", "at": now.isoformat(), "revision": row["revision"] + 1}][-60:]
        values = dict(status="queued", revision=row["revision"] + 1, retry_count=0,
                      lease_token=None, lease_expires_at=None, next_run_at=now,
                      checkpoint=checkpoint, result=result, events=events,
                      last_safe_error=None, updated_at=now)
        connection.execute(update(table).where(table.c.task_id == row["task_id"]).values(**values))
        pending_sync[row["task_id"]] = values


def _after_flush(session, flush_context):
    new_tasks = {obj.__dict__.get("task_id") for obj in session.new if isinstance(obj, MonthlyCloseTask)} - {None}
    session.info.setdefault(_NEW_TASKS_KEY, set()).update(new_tasks)
    cycle_ids, months = set(), set()
    all_cycles = False
    for obj in set(session.new) | set(session.dirty) | set(session.deleted):
        if getattr(obj, "__tablename__", None) not in EVIDENCE_TABLES:
            continue
        changed = _changed_columns(obj)
        if obj not in session.new and obj not in session.deleted and not changed:
            continue
        # A worker lease/heartbeat is not new evidence. Terminal processing and
        # payload changes are relevant even when the browser has already closed.
        if obj.__tablename__ == "monthly_close_processing_jobs" and not changed & {"status", "payload", "last_error_code"}:
            continue
        # Monitoring/presentation timestamps must not restart a completed task.
        if obj.__tablename__ == "monthly_close_cycles" and obj not in session.new and not changed & {"status", "final_snapshot", "write_control_owner", "billing_month"}:
            continue
        cycles = _scope_values(obj, "cycle_id")
        billing_months = _scope_values(obj, "billing_month")
        if cycles:
            cycle_ids.update(cycles)
        elif billing_months:
            months.update(billing_months)
        else:
            all_cycles = True
    if all_cycles or cycle_ids or months:
        _invalidate(session, cycle_ids=cycle_ids, months=months, all_cycles=all_cycles)


def _synchronize_loaded_tasks(session, flush_context=None):
    values_by_id = session.info.pop(_SYNC_KEY, {})
    if not values_by_id:
        return
    for obj in session.identity_map.values():
        if isinstance(obj, MonthlyCloseTask):
            values = values_by_id.get(obj.__dict__.get("task_id"))
            if values:
                for key, value in values.items():
                    attributes.set_committed_value(obj, key, value)


def _after_business_dml(execute_state):
    statement = execute_state.statement
    table = getattr(statement, "table", None)
    if not (execute_state.is_update or execute_state.is_delete or execute_state.is_insert) or getattr(table, "name", None) not in EVIDENCE_TABLES:
        return None
    result = execute_state.invoke_statement()
    if getattr(result, "rowcount", 1) != 0:
        # Bulk predicates can move records between months; conservatively cover
        # both old and new scopes instead of inspecting or guessing SQL clauses.
        _invalidate(execute_state.session, all_cycles=True)
        _synchronize_loaded_tasks(execute_state.session)
    return result


def _transaction_finished(session, transaction):
    if transaction.parent is None:
        session.info.pop(_NEW_TASKS_KEY, None)
        session.info.pop(_SYNC_KEY, None)
        session.info.pop(_TABLE_PRESENT_KEY, None)


def register_task_invalidation():
    # Imported by models in both API and Celery startup. Repeated imports and
    # test setup cannot add a second listener.
    for name, callback, options in (
        ("after_flush", _after_flush, {}),
        ("after_flush_postexec", _synchronize_loaded_tasks, {}),
        ("do_orm_execute", _after_business_dml, {"retval": True}),
        ("after_transaction_end", _transaction_finished, {}),
    ):
        if not event.contains(Session, name, callback):
            event.listen(Session, name, callback, **options)
