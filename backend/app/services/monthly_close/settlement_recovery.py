"""Prove an approved overwrite compensated a failed pending-bill generation.

Preserve the original attempt and failed verification. Only append recovery
evidence and close the exact technical issue; never change ledger or bill rows.
"""
from datetime import datetime, timezone
from uuid import uuid4

from sqlalchemy import and_, exists, select

from app.models.audit_log import AuditLog
from app.models.monthly_close import MonthlyCloseCycle
from app.models.monthly_close_control import (
    MonthlyCloseExecutionAttempt as Attempt,
    MonthlyCloseIssueInstance as Issue,
    MonthlyCloseProposal as Proposal,
    MonthlyCloseRemediation as Remediation,
    MonthlyCloseVerification as Verification,
)
from app.models.settlement import OwnerSettlement
from app.services.audit import log_action_tx
from app.services.monthly_close.control import (
    MonthlyCloseControlError, proposal_binding_is_valid, validate_request_id,
    verified_execution_lineage_is_valid,
)
from app.services.monthly_close.financial_lock import acquire_month_financial_lock
from app.services.monthly_close.verification import _current_admin, _digest

RECOVERY_VERSION = "settlement-replacement-v1"


def compensated_attempt_exists():
    """Only this separately verified, fully resolved recovery is nonblocking."""
    proof = exists(select(Verification.verification_id).join(
        Remediation, and_(Remediation.verification_id == Verification.verification_id,
                          Remediation.attempt_id == Verification.attempt_id,
                          Remediation.cycle_id == Verification.cycle_id),
    ).join(Proposal, Proposal.proposal_id == Attempt.proposal_id).where(
        Verification.attempt_id == Attempt.attempt_id,
        Verification.cycle_id == Attempt.cycle_id,
        Verification.verification_version == RECOVERY_VERSION,
        Verification.status == 'passed',
        Verification.checks['outcome'].as_string() == 'compensated',
        Verification.checks['original_attempt_id'].as_string() == Attempt.attempt_id,
        Verification.checks['replacement_proposal_id'].as_string() == Remediation.resolution_proposal_id,
        Remediation.status == 'resolved',
        Proposal.status == 'superseded',
    ).correlate(Attempt))
    open_remediation = exists(select(Remediation.remediation_id).where(
        Remediation.attempt_id == Attempt.attempt_id,
        Remediation.status != 'resolved',
    ).correlate(Attempt))
    return and_(Attempt.status == 'remediation_required', proof, ~open_remediation)


def _generation_plan(proposal):
    commands = (proposal.canonical_payload or {}).get('commands', [])
    if (proposal.proposal_type != 'generate_owner_settlements' or len(commands) != 1
            or commands[0].get('command_type') != 'generate_owner_settlements'
            or not proposal_binding_is_valid(proposal)):
        return None
    return commands[0]['after']


async def _generation_audit_matches(db, proposal, attempt):
    command = proposal.canonical_payload['commands'][0]
    plan = command['after']
    refs = attempt.audit_refs or []
    if len(refs) != 1 or not str(refs[0]).isdigit():
        return False
    audit = await db.get(AuditLog, int(refs[0]))
    after = audit.after_data if audit and isinstance(audit.after_data, dict) else {}
    expected_result = {
        'business_idempotency_key': command['business_idempotency_key'],
        'command_type': 'generate_owner_settlements', 'status': 'succeeded_unverified',
        'result': {'billing_month': plan['billing_month'], 'plan_hash': plan['plan_hash'],
                   'settlement_ids': plan['settlement_ids']},
    }
    return bool(audit and audit.action == 'monthly_close.settlements.execute'
                and audit.resource_id == proposal.cycle_id and audit.resource_type == 'monthly_close'
                and audit.operator_id == attempt.actor_id and audit.before_data == command['before']
                and after.get('approved_after') == plan
                and after.get('proposal_id') == proposal.proposal_id
                and after.get('request_id') == attempt.request_id
                and after.get('business_idempotency_key') == command['business_idempotency_key']
                and attempt.command_results == [expected_result])


async def _proven_replacement(db, cycle, original, original_attempt, candidates):
    from app.services.monthly_close.finalization import _settlement_plan_is_applied
    from app.services.settlement_preflight import run_settlement_preflight
    from app.api.v1.settlements import _settlement_digest

    old_plan = _generation_plan(original)
    if (old_plan is None or not old_plan.get('settlements')
            or old_plan.get('billing_month') != cycle.billing_month
            or original_attempt.proposal_binding_hash != original.proposal_binding_hash
            or any(item.get('status') != 'pending' for item in old_plan['settlements'])):
        return None
    old_ids = set(old_plan['settlement_ids'])
    if await db.scalar(select(OwnerSettlement.settlement_id).where(OwnerSettlement.settlement_id.in_(old_ids))):
        return None
    old_by_owner = {item['owner_id']: item['settlement_id'] for item in old_plan['settlements']}
    if len(old_by_owner) != len(old_ids):
        return None
    if not await _generation_audit_matches(db, original, original_attempt):
        return None
    for proposal, attempt, verification in candidates:
        plan = _generation_plan(proposal)
        if (plan is None or attempt.started_at is None or original_attempt.finished_at is None
                or attempt.started_at <= original_attempt.finished_at
                or not plan.get('overwrite') or plan.get('billing_month') != cycle.billing_month
                or proposal.subject_versions.get(cycle.cycle_id) != cycle.control_version):
            continue
        replacements = {item['owner_id']: item.get('replaces_settlement_id') for item in plan['settlements']}
        if any(replacements.get(owner) != old_id for owner, old_id in old_by_owner.items()):
            continue
        if not await verified_execution_lineage_is_valid(
            db, cycle_id=cycle.cycle_id, proposal_id=proposal.proposal_id,
            attempt_id=attempt.attempt_id, verification_id=verification.verification_id,
            proposal_type='generate_owner_settlements', lock=True,
        ):
            continue
        if not await _generation_audit_matches(db, proposal, attempt):
            continue
        if not await _settlement_plan_is_applied(db, plan):
            continue
        year, month = map(int, cycle.billing_month.split('-'))
        preflight = await run_settlement_preflight(db, year, month)
        if preflight.blocking or _settlement_digest(preflight.gate_dict()) != plan['preflight_hash']:
            continue
        return proposal, attempt, verification, old_ids
    return None


async def review_settlement_replacements(db, cycle, actor, *, request_id):
    request_id = validate_request_id(request_id)
    admin = await _current_admin(db, actor)
    cycle_id, month = cycle.cycle_id, cycle.billing_month
    await acquire_month_financial_lock(db, month)
    current = await db.scalar(select(MonthlyCloseCycle).where(
        MonthlyCloseCycle.cycle_id == cycle_id).with_for_update().execution_options(populate_existing=True))
    if current is None or current.status == 'completed' or current.write_control_owner != 'assistant':
        raise MonthlyCloseControlError('recovery_cycle_unavailable', '只能复查助手管理中的未关账月份')
    originals = (await db.execute(select(Proposal, Attempt).join(
        Attempt, Attempt.proposal_id == Proposal.proposal_id).where(
        Proposal.cycle_id == cycle_id, Proposal.proposal_type == 'generate_owner_settlements',
        Attempt.status == 'remediation_required', Proposal.status == 'approved',
    ).order_by(Attempt.attempt_id).with_for_update())).all()
    candidates = (await db.execute(select(Proposal, Attempt, Verification).join(
        Attempt, Attempt.proposal_id == Proposal.proposal_id).join(
        Verification, Verification.attempt_id == Attempt.attempt_id).where(
        Proposal.cycle_id == cycle_id, Proposal.proposal_type == 'generate_owner_settlements',
        Attempt.status == 'verified', Verification.status == 'passed',
    ).order_by(Proposal.created_at.desc()).with_for_update())).all()
    resolved = []
    for original, old_attempt in originals:
        remediations = list(await db.scalars(select(Remediation).where(
            Remediation.cycle_id == cycle_id, Remediation.attempt_id == old_attempt.attempt_id,
            Remediation.status != 'resolved').with_for_update()))
        if len(remediations) != 1:
            continue
        remediation = remediations[0]
        issue = await db.scalar(select(Issue).where(Issue.issue_id == remediation.issue_id).with_for_update())
        failure = await db.get(Verification, remediation.verification_id)
        if (issue is None or issue.adapter_type != 'execution_verification'
                or issue.source_subject_id != old_attempt.attempt_id
                or issue.issue_key != 'verification_not_proven' or issue.status not in {'open', 'rework'}
                or failure is None or failure.status not in {'failed', 'inconclusive'}
                or failure.attempt_id != old_attempt.attempt_id
                or failure.evidence_hash != issue.last_seen_evidence_hash):
            continue
        replacement = await _proven_replacement(db, current, original, old_attempt, candidates)
        if replacement is None:
            continue
        proposal, attempt, verification, old_ids = replacement
        now = datetime.now(timezone.utc)
        checks = {
            'outcome': 'compensated', 'request_id': request_id,
            'original_proposal_id': original.proposal_id, 'original_attempt_id': old_attempt.attempt_id,
            'original_failure_verification_id': failure.verification_id,
            'original_failure_evidence_hash': failure.evidence_hash,
            'replacement_proposal_id': proposal.proposal_id, 'replacement_attempt_id': attempt.attempt_id,
            'replacement_verification_id': verification.verification_id,
            'replacement_evidence_hash': verification.evidence_hash,
            'replaced_settlement_ids': sorted(old_ids),
            'complete_replacement': True, 'current_plan_matches': True,
            'original_attempt_replayed': False, 'financial_writes': False,
        }
        proof = Verification(
            verification_id=f'MCV-{uuid4().hex[:20].upper()}', cycle_id=cycle_id,
            attempt_id=old_attempt.attempt_id, verification_version=RECOVERY_VERSION,
            checks=checks, status='passed', verified_subject_versions={current.cycle_id: current.control_version},
            evidence_hash=_digest(checks), verified_at=now,
        )
        db.add(proof)
        await db.flush()
        note = '原生成异常已由另一个获批重算方案修复；新账单复核通过，原失败历史保留。'
        remediation.status = 'resolved'
        remediation.verification_id = proof.verification_id
        remediation.resolution_proposal_id = proposal.proposal_id
        remediation.evidence_refs = [checks]
        remediation.closed_at = now
        remediation.note = note
        issue.status = 'resolved'
        issue.verification_id = proof.verification_id
        issue.resolution_proposal_id = proposal.proposal_id
        issue.resolution_note = note
        original.status = 'superseded'  # forbid replay; original command/binding unchanged
        await log_action_tx(db, admin.user_id, 'monthly_close.settlements.recovery',
                            'monthly_close_execution_attempt', old_attempt.attempt_id,
                            after_data={**checks, 'verification_id': proof.verification_id})
        resolved.append(old_attempt.attempt_id)
    await db.commit()
    return {'billing_month': month, 'resolved_count': len(resolved),
            'remaining_count': len(originals) - len(resolved), 'resolved_attempt_ids': resolved}
