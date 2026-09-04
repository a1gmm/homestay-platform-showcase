"""Idempotent administrator alerts for stuck or stale monthly closes."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from hashlib import sha256
import json
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monthly_close import MonthlyCloseCycle
from app.models.notification import Notification, NotificationLog, NotificationType
from app.models.user import User, UserRole
from app.services.monthly_close.workflow import build_cycle_summary


def _utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=timezone.utc)


def _fingerprint(summary: dict) -> str:
    payload = {
        "status": summary["status"],
        "current_step": summary["current_step"],
        "blocking_count": summary["blocking_count"],
        "missing_sources": summary["missing_sources"],
        "inbox_pending_count": summary["inbox_pending_count"],
        "current_evidence_hash": summary.get("current_evidence_hash"),
        "blocking_issues": summary.get("blocking_issues", []),
    }
    return sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode()
    ).hexdigest()


async def scan_monthly_close_alerts(db: AsyncSession) -> int:
    """Create one notification per admin and evidence state, never mutate a cycle."""
    admins = list(
        (
            await db.execute(
                select(User).where(
                    User.role == UserRole.admin,
                    User.is_active.is_(True),
                )
            )
        ).scalars()
    )
    if not admins:
        return 0
    cycles = list((await db.execute(select(MonthlyCloseCycle))).scalars())
    now = datetime.now(timezone.utc)
    created = 0
    state_changed = False
    for cycle in cycles:
        summary = await build_cycle_summary(db, cycle)
        monitor_values = (
            summary["status"],
            summary["current_step"],
            summary["progress"],
            summary["blocking_count"],
        )
        stored_monitor_values = (
            cycle.monitor_effective_status,
            cycle.monitor_current_step,
            cycle.monitor_progress,
            cycle.monitor_blocking_count,
        )
        if monitor_values != stored_monitor_values:
            (
                cycle.monitor_effective_status,
                cycle.monitor_current_step,
                cycle.monitor_progress,
                cycle.monitor_blocking_count,
            ) = monitor_values
            state_changed = True
        reason: str | None = None
        title = ""
        content = ""
        if summary["status"] == "needs_recheck":
            reason = "needs_recheck"
            title = f"{cycle.billing_month} 月结完成后数据发生变化"
            content = (
                f"当前需要从“{summary['current_step_label']}”重新核对。"
                "原完成快照仍保留，请进入月结中心查看变化并填写原因重新打开。"
            )
        elif cycle.status != "completed":
            actionable = bool(
                summary["missing_source_count"]
                or summary["blocking_count"]
                or summary["inbox_pending_count"]
            )
            state_hash = _fingerprint(summary) if actionable else None
            if state_hash != cycle.monitor_state_hash:
                cycle.monitor_state_hash = state_hash
                cycle.monitor_state_since = now if actionable else None
                state_changed = True
            elif (
                actionable
                and cycle.monitor_state_since is not None
                and _utc(cycle.monitor_state_since) <= now - timedelta(days=1)
            ):
                reason = "stuck"
                title = f"{cycle.billing_month} 月结仍有待处理事项"
                parts = []
                if summary["missing_source_count"]:
                    parts.append(f"缺少 {summary['missing_source_count']} 类资料")
                if summary["inbox_pending_count"]:
                    parts.append(f"收件箱 {summary['inbox_pending_count']} 个文件待确认")
                if summary["blocking_count"]:
                    parts.append(f"当前步骤 {summary['blocking_count']} 个阻断项")
                content = "，".join(parts) + "。进入月结中心后按当前步骤继续处理。"
        if reason is None:
            continue
        state_hash = _fingerprint(summary)
        action_url = f"/finance/monthly-close?month={cycle.billing_month}"
        for admin in admins:
            marker_key = "mc:" + sha256(
                f"{cycle.cycle_id}:{admin.user_id}:{reason}".encode()
            ).hexdigest()[:32]
            dedupe_hash = sha256(f"{marker_key}:{state_hash}".encode()).hexdigest()
            try:
                async with db.begin_nested():
                    # A deterministic primary key makes overlapping Celery scans
                    # serialize at the database boundary. Only the winner can
                    # insert the paired notification in this savepoint.
                    db.add(
                        NotificationLog(
                            log_id="MCN" + dedupe_hash[:17].upper(),
                            template_name=marker_key,
                            channel="in_app",
                            recipient=admin.user_id,
                            content=state_hash,
                            status="sent",
                        )
                    )
                    db.add(
                        Notification(
                            notification_id="NTF" + dedupe_hash[:16].upper(),
                            user_id=admin.user_id,
                            title=title,
                            content=content,
                            action_url=action_url,
                            type=NotificationType.alert,
                            is_read=False,
                        )
                    )
                    await db.flush()
            except IntegrityError:
                continue
            created += 1
    if created or state_changed:
        await db.commit()
    return created
