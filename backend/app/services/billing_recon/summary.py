"""Deterministic live summaries and batch review state."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.recon import ReconBatch, ReconDiff, ReconDiffClass, ReconDiffStatus
from app.services.audit import log_action_tx


def build_live_summary(diffs: list[ReconDiff]) -> dict:
    total = len(diffs)
    pending = [diff for diff in diffs if diff.status == ReconDiffStatus.pending]
    pending_impact = Decimal("0")
    appeal_total = Decimal("0")
    for diff in pending:
        if diff.diff_class == ReconDiffClass.fix_amount and diff.bill_amount is not None and diff.system_amount is not None:
            pending_impact += abs(diff.system_amount - diff.bill_amount)
        elif diff.diff_class == ReconDiffClass.appeal and diff.system_amount is not None:
            pending_impact += abs(diff.system_amount)
            appeal_total += abs(diff.system_amount)
        elif diff.diff_class == ReconDiffClass.compensation and diff.bill_amount is not None:
            pending_impact += abs(diff.bill_amount)
    return {
        "total_actionable_count": total,
        "resolved_actionable_count": total - len(pending),
        "pending_count": len(pending),
        "pending_impact_total": str(pending_impact.quantize(Decimal("0.01"))),
        "appeal_total": str(appeal_total.quantize(Decimal("0.01"))),
    }


async def review_batch(db: AsyncSession, batch_id: str, user_id: str) -> dict | None:
    batch = (await db.execute(
        select(ReconBatch).where(ReconBatch.batch_id == batch_id).with_for_update()
    )).scalar_one_or_none()
    if batch is None:
        return None
    mapping = batch.mapping or {}
    if mapping.get("reviewed_at"):
        return {"reviewed_at": mapping["reviewed_at"], "reviewed_by": mapping.get("reviewed_by")}
    diffs = (await db.execute(
        select(ReconDiff).where(ReconDiff.batch_id == batch_id)
    )).scalars().all()
    if any(diff.status == ReconDiffStatus.pending for diff in diffs):
        raise ValueError("仍有待处理差异，暂不能确认本月已核对")
    reviewed_at = datetime.now(timezone.utc).isoformat()
    batch.mapping = {**mapping, "reviewed_at": reviewed_at, "reviewed_by": user_id}
    await log_action_tx(
        db, user_id, "billing_recon.review", "recon_batch", batch.batch_id,
        after_data={"reviewed_at": reviewed_at},
    )
    await db.commit()
    return {"reviewed_at": reviewed_at, "reviewed_by": user_id}
