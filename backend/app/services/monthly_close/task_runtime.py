"""Durable task scheduling, separate from financial execution transactions.

``advance(db, cycle, actor, checkpoint)`` may read evidence and prepare idempotent
proposals. It must never infer authority to execute a financial write from a goal.
Proposal execution remains in the existing explicit approval path. The callback
must re-read committed action outcomes after a crash, rather than replaying them.
"""
from __future__ import annotations

import asyncio
import copy
import hashlib
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import and_, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monthly_close import MonthlyCloseCycle
from app.models.monthly_close_task import MonthlyCloseTask
from app.models.user import User, UserRole
from app.services.monthly_close.permissions import (
    MonthlyCloseFeature, MonthlyClosePermissionDenied,
    assert_monthly_close_feature_enabled,
)

LEASE_SECONDS = 180
ADVANCE_TIMEOUT_SECONDS = 120
MAX_RETRIES = 3
MAX_EVENTS = 60
MAX_CONTINUOUS_STEPS = 30
RUNNABLE_RESULTS = {"queued", "waiting_user", "waiting_approval", "paused", "succeeded", "failed"}


def _now():
    return datetime.now(timezone.utc)


def _event(row, kind: str):
    row.events = [*(row.events or []), {"kind": kind, "at": _now().isoformat(), "revision": row.revision}][-MAX_EVENTS:]
    row.updated_at = _now()


def _automation_enabled():
    assert_monthly_close_feature_enabled(MonthlyCloseFeature.natural_language)
    assert_monthly_close_feature_enabled(MonthlyCloseFeature.low_risk_automation)


async def _context(db, cycle_id, actor_id):
    # populate_existing defeats stale identity-map authorization after demotion.
    actor = await db.scalar(select(User).where(User.user_id == actor_id).execution_options(populate_existing=True))
    if actor is None or not actor.is_active or actor.role != UserRole.admin:
        raise HTTPException(403, "月结任务仅限当前有效的管理员账号")
    cycle = await db.scalar(select(MonthlyCloseCycle).where(MonthlyCloseCycle.cycle_id == cycle_id).execution_options(populate_existing=True))
    if cycle is None or cycle.status not in {"open", "reopened", "completed"}:
        raise HTTPException(404, "月结月份不存在或状态无效")
    return cycle, {"user_id": actor.user_id, "role": "admin", "is_active": True}


def serialize_task(row):
    if row is None:
        return None
    result = row.result or {}
    activity = {"queued": "等待后台继续处理", "running": "正在核对资料并推进任务", "waiting_user": "等待补充事实或资料", "waiting_approval": "等待审核方案", "paused": "任务已暂停", "succeeded": "交付已核验", "failed": "任务遇到问题，需要处理"}[row.status]
    return {
        "task_id": row.task_id, "revision": row.revision, "status": row.status,
        "goal": row.goal, "summary": result.get("summary", ""),
        "steps": result.get("steps", []), "questions": result.get("questions", []),
        "actions": result.get("actions", []), "artifacts": result.get("artifacts", []),
        "evidence_hash": result.get("evidence_hash"),
        "current_activity": activity, "activity": activity, "last_safe_error": row.last_safe_error,
        "retry_count": row.retry_count, "updated_at": row.updated_at,
    }


async def _row(db, cycle_id, actor_id, *, lock=False):
    statement = select(MonthlyCloseTask).where(MonthlyCloseTask.cycle_id == cycle_id, MonthlyCloseTask.actor_id == actor_id).execution_options(populate_existing=True)
    if lock:
        statement = statement.with_for_update()
    return await db.scalar(statement)


def _revision(row, expected_revision):
    if expected_revision is not None and (row is None or row.revision != expected_revision):
        raise HTTPException(409, "任务已更新，请刷新后重试")


async def read_task(db: AsyncSession, cycle_id: str, actor_id: str):
    await _context(db, cycle_id, actor_id)
    return serialize_task(await _row(db, cycle_id, actor_id))


async def start_task(db, cycle_id, actor_id, goal, *, expected_revision=None):
    cycle, _ = await _context(db, cycle_id, actor_id)
    try:
        _automation_enabled()
    except MonthlyClosePermissionDenied as exc:
        raise HTTPException(503, exc.message) from exc
    goal = str(goal or "").strip()
    if not goal or len(goal) > 4000:
        raise HTTPException(422, "请提供 1 至 4000 字的任务目标")
    from app.services.monthly_close.task_chat import has_unsupported_task_scope, validate_task_month
    if has_unsupported_task_scope(goal):
        raise HTTPException(422, "整月任务按已确认规则核对本月全部资料。单项范围、费用承担方或删除要求请先在聊天中处理，再开始整月核对。")
    validate_task_month(goal, cycle.billing_month)
    # Serializes first creation as well as replacements for this cycle. The unique
    # constraint is the final defense against duplicate starts.
    await db.scalar(select(MonthlyCloseCycle).where(MonthlyCloseCycle.cycle_id == cycle_id).with_for_update())
    row = await _row(db, cycle_id, actor_id, lock=True)
    _revision(row, expected_revision)
    if row and row.status in {"queued", "running", "waiting_user", "waiting_approval"}:
        if row.goal != goal:
            raise HTTPException(409, "已有月结任务，请先暂停当前任务再更换目标")
        return serialize_task(row)
    if row is None:
        row = MonthlyCloseTask(task_id=uuid4().hex, cycle_id=cycle_id, actor_id=actor_id, goal=goal, revision=1, generation=1, events=[])
        db.add(row)
    else:
        row.revision += 1
        row.generation += 1
    row.goal = goal
    row.status = "queued"
    row.retry_count = 0
    row.checkpoint = {}
    row.result = {}
    row.last_safe_error = None
    row.next_run_at = _now()
    row.lease_token = None
    row.lease_expires_at = None
    _event(row, "started")
    await db.commit()
    return serialize_task(row)


async def resume_task(db, cycle_id, actor_id, *, expected_revision=None):
    await _context(db, cycle_id, actor_id)
    try:
        _automation_enabled()
    except MonthlyClosePermissionDenied as exc:
        raise HTTPException(503, exc.message) from exc
    row = await _row(db, cycle_id, actor_id, lock=True)
    if row is None:
        raise HTTPException(404, "月结任务不存在")
    _revision(row, expected_revision)
    if row.status in {"queued", "running", "succeeded"}:
        return serialize_task(row)
    row.status = "queued"
    row.revision += 1
    row.retry_count = 0
    row.last_safe_error = None
    row.lease_token = None
    row.lease_expires_at = None
    row.next_run_at = _now()
    row.checkpoint = {**(row.checkpoint or {}), "continuous_steps": 0}
    _event(row, "resumed")
    await db.commit()
    return serialize_task(row)


async def pause_task(db, cycle_id, actor_id, *, expected_revision=None):
    await _context(db, cycle_id, actor_id)
    row = await _row(db, cycle_id, actor_id, lock=True)
    if row is None:
        raise HTTPException(404, "月结任务不存在")
    _revision(row, expected_revision)
    if row.status not in {"paused", "succeeded"}:
        row.status = "paused"
        row.revision += 1
        row.lease_token = None
        row.lease_expires_at = None
        row.next_run_at = None
        _event(row, "paused")
        await db.commit()
    return serialize_task(row)


async def mark_dirty(db, cycle_id, actor_id=None, *, commit=True):
    """Wake only tasks awaiting evidence/approval, preserving explicit pauses.

    Use commit=False when attaching this to an existing evidence transaction.
    Dirtying a running task revokes its result fence; its safe preparation may
    finish, but the next pass must re-read all facts before publishing a result.
    """
    stmt = select(MonthlyCloseTask).where(MonthlyCloseTask.cycle_id == cycle_id, MonthlyCloseTask.status.in_(["waiting_user", "waiting_approval", "running", "queued", "succeeded"])).with_for_update().execution_options(populate_existing=True)
    if actor_id is not None:
        stmt = stmt.where(MonthlyCloseTask.actor_id == actor_id)
    rows = list((await db.scalars(stmt)).all())
    for row in rows:
        if row.status == "queued" and (row.checkpoint or {}).get("evidence_dirty") and not row.lease_token:
            continue
        was_succeeded = row.status == "succeeded"
        row.status = "queued"
        row.revision += 1
        row.retry_count = 0
        row.lease_token = None
        row.lease_expires_at = None
        row.next_run_at = _now()
        row.checkpoint = {**(row.checkpoint or {}), "continuous_steps": 0, "evidence_dirty": True}
        if was_succeeded:
            row.checkpoint = {**row.checkpoint, "delivery_verified": False}
            row.result = {**(row.result or {}), "summary": "资料已更新，正在重新核验交付。", "artifacts": [], "evidence_hash": ""}
        _event(row, "evidence_changed")
    if commit:
        await db.commit()
    return len(rows)


def _due(now):
    return or_(
        and_(MonthlyCloseTask.status == "queued", or_(MonthlyCloseTask.next_run_at.is_(None), MonthlyCloseTask.next_run_at <= now)),
        and_(MonthlyCloseTask.status == "running", MonthlyCloseTask.lease_expires_at <= now),
    )


async def pending_task_ids(db, *, limit=20):
    return list((await db.scalars(select(MonthlyCloseTask.task_id).where(_due(_now())).order_by(MonthlyCloseTask.updated_at).limit(limit))).all())


async def _claim(db, task_id):
    row = await db.scalar(select(MonthlyCloseTask).where(MonthlyCloseTask.task_id == task_id, _due(_now())).with_for_update(skip_locked=True).execution_options(populate_existing=True))
    if row is None:
        return None
    try:
        await _context(db, row.cycle_id, row.actor_id)
    except HTTPException:
        row.status = "failed"
        row.last_safe_error = "管理员权限或月份状态已变化。请刷新页面并由有效管理员重新发起任务。"
        row.revision += 1
        row.lease_token = None
        row.lease_expires_at = None
        _event(row, "authorization_failed")
        await db.commit()
        return None
    try:
        _automation_enabled()
    except MonthlyClosePermissionDenied as exc:
        row.status = "paused"
        row.last_safe_error = exc.message
        row.revision += 1
        row.lease_token = None
        row.lease_expires_at = None
        _event(row, "automation_disabled")
        await db.commit()
        return None
    recovering = row.status == "running"
    if recovering:
        row.retry_count += 1
        if row.retry_count >= MAX_RETRIES:
            row.status = "failed"
            row.last_safe_error = "后台任务连续中断。已保留进度，请检查后台服务后点击继续。"
            row.revision += 1
            row.lease_token = None
            row.lease_expires_at = None
            _event(row, "recovery_exhausted")
            await db.commit()
            return None
    previous_revision = row.revision
    token = uuid4().hex
    # Compare-and-swap complements PostgreSQL row locks and fences every result.
    updated = await db.execute(update(MonthlyCloseTask).where(MonthlyCloseTask.task_id == task_id, MonthlyCloseTask.revision == previous_revision, _due(_now())).values(status="running", revision=previous_revision + 1, retry_count=row.retry_count, lease_token=token, lease_expires_at=_now() + timedelta(seconds=LEASE_SECONDS), updated_at=_now()).execution_options(synchronize_session=False))
    if updated.rowcount != 1:
        await db.rollback()
        return None
    await db.refresh(row)
    _event(row, "recovered" if recovering else "claimed")
    snapshot = {"task_id": row.task_id, "cycle_id": row.cycle_id, "actor_id": row.actor_id, "goal": row.goal, "generation": row.generation, "revision": row.revision, "token": token, "checkpoint": copy.deepcopy(row.checkpoint or {})}
    await db.commit()
    return snapshot


async def _finish(db, claim, result=None, *, failed=False):
    row = await db.scalar(select(MonthlyCloseTask).where(MonthlyCloseTask.task_id == claim["task_id"], MonthlyCloseTask.lease_token == claim["token"], MonthlyCloseTask.revision == claim["revision"], MonthlyCloseTask.status == "running", MonthlyCloseTask.lease_expires_at > _now()).with_for_update().execution_options(populate_existing=True))
    if row is None:
        return "superseded"
    if failed:
        row.retry_count += 1
        row.status = "failed" if row.retry_count >= MAX_RETRIES else "queued"
        row.last_safe_error = "后台处理暂时失败，正在重试。" if row.status == "queued" else "后台处理连续失败。已保留进度，请检查资料和后台服务后点击继续。"
        row.next_run_at = _now() + timedelta(seconds=15 * 2 ** row.retry_count)
    else:
        checkpoint = copy.deepcopy(result.get("checkpoint") or {})
        row.status = result["status"]
        continuous_steps = int((row.checkpoint or {}).get("continuous_steps", 0)) + 1
        if row.status == "queued" and continuous_steps >= MAX_CONTINUOUS_STEPS:
            row.status = "waiting_user"
            result = {**result, "summary": "自动处理达到本轮上限，已保存进度。请检查当前资料并点击继续。", "questions": [{"key": "step_limit", "subject": "本轮处理上限", "message": "请检查本轮结果后继续任务。"}]}
        checkpoint["continuous_steps"] = continuous_steps
        row.checkpoint = checkpoint
        row.result = {key: copy.deepcopy(result.get(key, [] if key in {"steps", "questions", "actions", "artifacts"} else "")) for key in ("summary", "steps", "questions", "actions", "artifacts", "evidence_hash")}
        row.last_safe_error = None
        row.retry_count = 0
        row.next_run_at = _now() if row.status == "queued" else None
    row.revision += 1
    row.lease_token = None
    row.lease_expires_at = None
    _event(row, "retry" if failed and row.status == "queued" else row.status)
    await db.commit()
    return row.status


async def execute_task(task_id, advance, *, session_factory=None):
    """Run one bounded, recoverable step without holding a finance row lock.

    PostgreSQL transaction advisory locks are held on a separate connection so
    callback commits cannot release execution exclusion. Lease/revision fencing
    prevents pause, replacement, or newly committed evidence being overwritten.
    """
    if session_factory is None:
        from app.core.database import AsyncSessionLocal
        session_factory = AsyncSessionLocal
    async with session_factory() as guard:
        if guard.bind.dialect.name == "postgresql":
            lock_key = int.from_bytes(hashlib.sha256(("monthly-close-task:" + task_id).encode()).digest()[:8], "big", signed=True)
            acquired = await guard.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": lock_key})
            if not acquired:
                return {"task_id": task_id, "status": "busy"}
        async with session_factory() as state_db:
            claim = await _claim(state_db, task_id)
        if claim is None:
            return {"task_id": task_id, "status": "not_runnable"}
        try:
            async with session_factory() as work_db:
                cycle, actor = await _context(work_db, claim["cycle_id"], claim["actor_id"])
                _automation_enabled()
                still_claimed = await work_db.scalar(select(MonthlyCloseTask.task_id).where(MonthlyCloseTask.task_id == task_id, MonthlyCloseTask.lease_token == claim["token"], MonthlyCloseTask.revision == claim["revision"], MonthlyCloseTask.status == "running"))
                if still_claimed is None:
                    return {"task_id": task_id, "status": "superseded"}
                checkpoint = {**claim["checkpoint"], "task_id": task_id, "goal": claim["goal"], "actor_id": claim["actor_id"], "generation": claim["generation"]}
                result = await asyncio.wait_for(advance(work_db, cycle, actor, checkpoint), timeout=ADVANCE_TIMEOUT_SECONDS)
                if not isinstance(result, dict) or result.get("status") not in RUNNABLE_RESULTS:
                    raise ValueError("invalid_task_result")
                if not isinstance(result.get("checkpoint", {}), dict):
                    raise ValueError("invalid_task_checkpoint")
                if result["status"] == "succeeded" and result.get("checkpoint", {}).get("delivery_verified") is not True:
                    raise ValueError("delivery_verification_required")
                # Persist permitted proposal preparation before task completion;
                # an interrupted checkpoint must re-read these committed records.
                await _context(work_db, claim["cycle_id"], claim["actor_id"])
                _automation_enabled()
                await work_db.commit()
            async with session_factory() as state_db:
                status = await _finish(state_db, claim, result)
        except Exception:
            # Exception strings can contain source data/provider credentials.
            # Only bounded, actionable server-owned errors are persisted.
            async with session_factory() as state_db:
                status = await _finish(state_db, claim, failed=True)
        return {"task_id": task_id, "status": status}
