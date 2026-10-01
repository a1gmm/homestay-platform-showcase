"""Read-only, evidence-pinned pages; durable chat contains previews only."""

from app.services.monthly_close.conversation import _detail_next_step
from app.services.monthly_close.evidence import build_role_workflow_evidence
from app.services.monthly_close.workflow import MonthlyCloseConflict


async def read_progress_details(
    db, cycle, *, actor_role, step_key, offset=0, limit=20,
    expected_evidence_hash=None,
):
    # Gate before any financial evidence query, including internal callers.
    if actor_role != "admin":
        raise PermissionError("仅管理员可查看完整月结事项")
    if offset < 0 or not 1 <= limit <= 50:
        raise ValueError("分页范围无效")
    evidence = await build_role_workflow_evidence(db, cycle, actor_role=actor_role)
    step = next((row for row in evidence if row["step_key"] == step_key), None)
    if step is None:
        raise LookupError("月结步骤不存在")
    if expected_evidence_hash and expected_evidence_hash != step["evidence_hash"]:
        raise MonthlyCloseConflict(
            "progress_evidence_changed", "事项已变化，请从第一页重新读取当前结果。"
        )
    issues = step.get("issues", [])
    return {
        "billing_month": cycle.billing_month,
        "cycle_id": cycle.cycle_id,
        "step_key": step_key,
        "evidence_hash": step["evidence_hash"],
        "result_state": "current",
        "total": len(issues),
        "offset": offset,
        "limit": limit,
        "has_more": offset + limit < len(issues),
        # Preserve complete evidence fields, including failed rows and adapter
        # navigation actions. Never silently clip individual explanations.
        "items": [
            {**issue, "next_step": _detail_next_step(issue), "ordinal": index + 1}
            for index, issue in enumerate(issues[offset:offset + limit], offset)
        ],
    }
