"""Read-only, evidence-pinned pages and complete export data."""
from app.services.monthly_close.conversation import _detail_next_step, LABELS
from app.services.monthly_close.evidence import build_role_workflow_evidence
from app.services.monthly_close.workflow import MonthlyCloseConflict


async def read_progress_snapshot(db, cycle, *, actor_role, step_key,
                                 expected_evidence_hash=None, document_id=None, actor=None):
    if actor_role != 'admin':
        raise PermissionError('仅管理员可查看完整月结事项')
    if step_key in LABELS:
        from app.services.monthly_close.conversation import read_progress
        from app.services.monthly_close.projection import build_monthly_close_projection
        if not actor:
            raise PermissionError('缺少当前查询身份')
        projection = await build_monthly_close_projection(db, cycle, actor)
        facts = await read_progress(db, cycle, projection, '逐项查询差异', step_key,
            mode='details', document_ids=[document_id] if document_id else None, detail_limit=None)
        if facts.detail_total is None:
            raise LookupError('没有唯一可读取的明细表，请重新选择文件')
        evidence_hash = facts.detail_evidence_hash
        items = [{**item.model_dump(exclude_none=True), 'resource_id': str(item.ordinal),
                  'code': item.issue_code, 'message': item.cause} for item in facts.detail_items or []]
    else:
        evidence = await build_role_workflow_evidence(db, cycle, actor_role=actor_role)
        step = next((row for row in evidence if row['step_key'] == step_key), None)
        if step is None:
            raise LookupError('月结步骤不存在')
        evidence_hash = step['evidence_hash']
        items = [{**issue, 'next_step': _detail_next_step(issue), 'ordinal': index + 1}
                 for index, issue in enumerate(step.get('issues', []))]
    if expected_evidence_hash and expected_evidence_hash != evidence_hash:
        raise MonthlyCloseConflict('progress_evidence_changed', '事项已变化，请从第一页重新读取当前结果。')
    return evidence_hash, items


async def read_progress_details(db, cycle, *, actor_role, step_key, offset=0, limit=20,
                                expected_evidence_hash=None, document_id=None, actor=None):
    # Gate before any evidence query, even when pagination is invalid.
    if actor_role != 'admin':
        raise PermissionError('仅管理员可查看完整月结事项')
    if offset < 0 or not 1 <= limit <= 50:
        raise ValueError('分页范围无效')
    evidence_hash, items = await read_progress_snapshot(db, cycle, actor_role=actor_role,
        step_key=step_key, expected_evidence_hash=expected_evidence_hash, document_id=document_id, actor=actor)
    return dict(billing_month=cycle.billing_month, cycle_id=cycle.cycle_id, step_key=step_key,
        evidence_hash=evidence_hash, result_state='current', total=len(items), offset=offset,
        limit=limit, has_more=offset + limit < len(items), items=items[offset:offset + limit])
