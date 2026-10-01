"""Independent deterministic verification for monthly-close execution attempts."""

from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.exc import NoInspectionAvailable, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monthly_close import MonthlyCloseCycle
from app.models.monthly_close_control import (
    MonthlyCloseExecutionAttempt,
    MonthlyCloseIssueInstance,
    MonthlyCloseProposal,
    MonthlyCloseRemediation,
    MonthlyCloseVerification,
)
from app.models.user import User
from app.services.audit import log_action_tx
from app.services.monthly_close.control import (
    CommandHandlerRegistry,
    MonthlyCloseControlError,
    execution_lineage_hash,
    get_command_handler_registry,
    proposal_binding_is_valid,
    validate_request_id,
    verified_execution_evidence_hash,
)


HASH_LENGTH = 64


class _StrictEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SubjectEvidence(_StrictEvidence):
    subject_id: str
    expected_version: Any
    actual_version: Any


class TargetEvidence(_StrictEvidence):
    expected_before: dict[str, Any]
    expected_after: dict[str, Any]
    actual: dict[str, Any]


class AmountEvidence(_StrictEvidence):
    expected: Decimal
    actual: Decimal

    @field_validator("expected", "actual", mode="before")
    @classmethod
    def exact_money(cls, value: Any) -> Decimal:
        if isinstance(value, float):
            raise ValueError("floating point verification money is not accepted")
        try:
            return Decimal(str(value)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("verification money is invalid") from exc


class AuditEvidence(_StrictEvidence):
    present: bool
    request_id: str
    business_idempotency_key: str
    expected_before: dict[str, Any]
    expected_after: dict[str, Any]


class IdempotencyEvidence(_StrictEvidence):
    present: bool
    request_id: str
    business_idempotency_key: str


class SourceEvidence(_StrictEvidence):
    approved_evidence_hash: str
    verification_evidence_hash: str

    @field_validator("approved_evidence_hash", "verification_evidence_hash")
    @classmethod
    def sha256_digest(cls, value: str) -> str:
        if (
            not isinstance(value, str)
            or len(value) != HASH_LENGTH
            or any(character not in "0123456789abcdef" for character in value)
        ):
            raise ValueError("evidence hash must be lowercase SHA-256 hex")
        return value


class IssueEvidence(_StrictEvidence):
    issue_id: str
    approved_evidence_hash: str
    actual_evidence_hash: str
    resolution: Literal["resolve", "keep_open"] = "resolve"

    @field_validator("approved_evidence_hash", "actual_evidence_hash")
    @classmethod
    def sha256_digest(cls, value: str) -> str:
        return SourceEvidence.sha256_digest(value)


class CommandVerificationEvidence(_StrictEvidence):
    outcome: Literal["applied", "not_applied", "inconclusive"]
    business_idempotency_key: str
    subject: SubjectEvidence
    target: TargetEvidence
    amount: AmountEvidence
    audit: AuditEvidence
    idempotency: IdempotencyEvidence
    evidence: SourceEvidence
    issues: list[IssueEvidence]


def _identifier(prefix: str, body_length: int) -> str:
    return prefix + uuid4().hex[:body_length].upper()


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()


async def _current_admin(db: AsyncSession, actor: Any) -> User:
    if isinstance(actor, dict):
        actor_id = actor.get("user_id")
    else:
        try:
            identity = sa_inspect(actor).identity
        except NoInspectionAvailable:
            identity = None
        actor_id = identity[0] if identity else getattr(actor, "user_id", None)
    user = (
        await db.scalar(
            select(User)
            .where(User.user_id == actor_id)
            .execution_options(populate_existing=True)
        )
        if isinstance(actor_id, str)
        else None
    )
    if (
        user is None
        or not user.is_active
        or getattr(user.role, "value", user.role) != "admin"
    ):
        raise MonthlyCloseControlError(
            "verification_forbidden", "当前账号无权复核财务执行"
        )
    return user


def _expected_subject_version(
    proposal: MonthlyCloseProposal, command: dict[str, Any]
) -> Any:
    subject_id = command["subject_id"]
    if subject_id in proposal.subject_versions:
        return proposal.subject_versions[subject_id]
    if subject_id == proposal.cycle_id and "cycle" in proposal.subject_versions:
        return proposal.subject_versions["cycle"]
    return None


def _expected_actual_version(expected_version: Any, command: dict[str, Any]) -> Any:
    after = command["after"]
    if "subject_version" in after:
        return after["subject_version"]
    if "control_version" in after:
        return after["control_version"]
    return expected_version


def _approved_issue_refs(command: dict[str, Any]) -> dict[str, str] | None:
    linked: dict[str, str] = {}
    for ref in command.get("evidence_refs", []):
        if not isinstance(ref, dict) or ref.get("kind") != "issue":
            continue
        issue_id = ref.get("issue_id")
        evidence_hash = ref.get("evidence_hash")
        if (
            not isinstance(issue_id, str)
            or not issue_id
            or not isinstance(evidence_hash, str)
            or len(evidence_hash) != HASH_LENGTH
        ):
            return None
        if issue_id in linked and linked[issue_id] != evidence_hash:
            return None
        linked[issue_id] = evidence_hash
    return linked


async def _normalize_result(
    db: AsyncSession,
    raw: dict[str, Any],
    *,
    proposal: MonthlyCloseProposal,
    command: dict[str, Any],
    attempt: MonthlyCloseExecutionAttempt,
) -> dict[str, Any]:
    try:
        facts = CommandVerificationEvidence.model_validate(raw)
    except (ValidationError, TypeError):
        return {
            "outcome": "inconclusive",
            "checks": {"typed_evidence_valid": False},
            "evidence_hash": _digest(
                {
                    "attempt_id": attempt.attempt_id,
                    "business_idempotency_key": command[
                        "business_idempotency_key"
                    ],
                    "reason": "typed_evidence_invalid",
                }
            ),
            "verified_subject_versions": {},
            "linked_issues": [],
            "kept_open_issues": [],
        }

    expected_version = _expected_subject_version(proposal, command)
    expected_actual_version = _expected_actual_version(expected_version, command)
    command_amount = Decimal(str(command["amount_impact"])).quantize(Decimal("0.01"))
    identity_matches = (
        facts.business_idempotency_key
        == command["business_idempotency_key"]
        and facts.subject.subject_id == command["subject_id"]
        and facts.subject.expected_version == expected_version
        and facts.subject.actual_version == expected_actual_version
        and facts.target.expected_before == command["before"]
        and facts.target.expected_after == command["after"]
        and facts.amount.expected == command_amount
        and facts.audit.request_id == attempt.request_id
        and facts.audit.business_idempotency_key
        == command["business_idempotency_key"]
        and facts.audit.expected_before == command["before"]
        and facts.audit.expected_after == command["after"]
        and facts.idempotency.request_id == attempt.request_id
        and facts.idempotency.business_idempotency_key
        == command["business_idempotency_key"]
        and facts.evidence.approved_evidence_hash == proposal.evidence_hash
    )

    approved_issue_refs = _approved_issue_refs(command)
    linked_issues: list[MonthlyCloseIssueInstance] = []
    kept_open_issues: list[MonthlyCloseIssueInstance] = []
    issue_evidence_matches = approved_issue_refs is not None
    returned_issues = {item.issue_id: item for item in facts.issues}
    if (
        approved_issue_refs is None
        or len(returned_issues) != len(facts.issues)
        or set(returned_issues) != set(approved_issue_refs)
    ):
        issue_evidence_matches = False
    elif approved_issue_refs:
        rows = list(
            await db.scalars(
                select(MonthlyCloseIssueInstance)
                .where(
                    MonthlyCloseIssueInstance.cycle_id == proposal.cycle_id,
                    MonthlyCloseIssueInstance.resolution_proposal_id
                    == proposal.proposal_id,
                    MonthlyCloseIssueInstance.issue_id.in_(approved_issue_refs),
                )
                .with_for_update()
            )
        )
        rows_by_id = {row.issue_id: row for row in rows}
        issue_evidence_matches = len(rows_by_id) == len(approved_issue_refs)
        for issue_id, expected_hash in approved_issue_refs.items():
            issue = rows_by_id.get(issue_id)
            returned = returned_issues.get(issue_id)
            if (
                issue is None
                or returned is None
                or returned.approved_evidence_hash != expected_hash
                or returned.actual_evidence_hash != expected_hash
                or issue.last_seen_evidence_hash != expected_hash
                or issue.status in {"resolved", "cancelled"}
            ):
                issue_evidence_matches = False
                continue
            if returned.resolution == "keep_open":
                kept_open_issues.append(issue)
            else:
                linked_issues.append(issue)

    effect_matches = False
    if facts.outcome == "applied":
        effect_matches = (
            facts.target.actual == command["after"]
            and facts.amount.actual == command_amount
            and facts.audit.present
            and facts.idempotency.present
        )
    elif facts.outcome == "not_applied":
        effect_matches = (
            facts.target.actual == command["before"]
            and facts.amount.actual == Decimal("0.00")
            and not facts.audit.present
            and not facts.idempotency.present
        )

    typed_evidence_valid = (
        identity_matches and issue_evidence_matches and effect_matches
    )
    outcome = facts.outcome if typed_evidence_valid else "inconclusive"
    return {
        "outcome": outcome,
        "checks": {
            "audit_present": facts.audit.present,
            "business_identity_matches": identity_matches,
            "idempotency_present": facts.idempotency.present,
            "issue_evidence_matches": issue_evidence_matches,
            "target_and_amount_match": effect_matches,
            "typed_evidence_valid": typed_evidence_valid,
        },
        "evidence_hash": facts.evidence.verification_evidence_hash,
        "verified_subject_versions": (
            {facts.subject.subject_id: facts.subject.actual_version}
            if typed_evidence_valid
            else {}
        ),
        "linked_issues": linked_issues if typed_evidence_valid else [],
        "kept_open_issues": kept_open_issues if typed_evidence_valid else [],
    }


async def _ensure_failure_remediation(
    db: AsyncSession,
    attempt: MonthlyCloseExecutionAttempt,
    verification: MonthlyCloseVerification,
) -> None:
    remediation = await db.scalar(
        select(MonthlyCloseRemediation).where(
            MonthlyCloseRemediation.attempt_id == attempt.attempt_id
        )
    )
    if remediation is not None:
        remediation.status = "rework"
        remediation.verification_id = verification.verification_id
        return
    issue = await db.scalar(
        select(MonthlyCloseIssueInstance).where(
            MonthlyCloseIssueInstance.cycle_id == attempt.cycle_id,
            MonthlyCloseIssueInstance.adapter_type == "execution_verification",
            MonthlyCloseIssueInstance.source_subject_id == attempt.attempt_id,
            MonthlyCloseIssueInstance.issue_key == "verification_not_proven",
        )
    )
    if issue is None:
        issue = MonthlyCloseIssueInstance(
            issue_id=_identifier("MCI-", 20),
            cycle_id=attempt.cycle_id,
            adapter_type="execution_verification",
            source_subject_id=attempt.attempt_id,
            issue_key="verification_not_proven",
            status="open",
            last_seen_evidence_hash=verification.evidence_hash,
        )
        db.add(issue)
        await db.flush()
    db.add(
        MonthlyCloseRemediation(
            remediation_id=_identifier("MCRM-", 19),
            cycle_id=attempt.cycle_id,
            attempt_id=attempt.attempt_id,
            issue_id=issue.issue_id,
            status="rework",
            observed_state="deterministic verification did not prove the approved result",
            evidence_refs=[],
            verification_id=verification.verification_id,
        )
    )


async def _resolve_verified_issues(
    db: AsyncSession,
    verification: MonthlyCloseVerification,
    issues: list[MonthlyCloseIssueInstance],
) -> None:
    if not issues:
        return
    for issue in issues:
        issue.status = "resolved"
        issue.verification_id = verification.verification_id
        issue.resolution_note = "deterministic verification passed"
        remediations = list(
            await db.scalars(
                select(MonthlyCloseRemediation)
                .where(
                    MonthlyCloseRemediation.cycle_id == verification.cycle_id,
                    MonthlyCloseRemediation.issue_id == issue.issue_id,
                    MonthlyCloseRemediation.attempt_id == verification.attempt_id,
                    MonthlyCloseRemediation.resolution_proposal_id
                    == issue.resolution_proposal_id,
                    MonthlyCloseRemediation.status != "resolved",
                )
                .with_for_update()
            )
        )
        for remediation in remediations:
            evidence_refs = (
                remediation.evidence_refs
                if isinstance(remediation.evidence_refs, list)
                else []
            )
            exact_evidence = any(
                isinstance(ref, dict)
                and ref.get("issue_id") == issue.issue_id
                and ref.get("evidence_hash") == issue.last_seen_evidence_hash
                for ref in evidence_refs
            )
            if not exact_evidence:
                continue
            remediation.status = "resolved"
            remediation.verification_id = verification.verification_id
            remediation.closed_at = verification.verified_at


async def _keep_verified_issues_open(
    verification: MonthlyCloseVerification,
    issues: list[MonthlyCloseIssueInstance],
) -> None:
    for issue in issues:
        issue.status = "waiting_information"
        issue.verification_id = verification.verification_id
        issue.resolution_note = "确定性动作已复核；OTA差异仍在等待平台或人工后续结果"


async def verify_attempt(
    db: AsyncSession,
    attempt: MonthlyCloseExecutionAttempt,
    actor: Any,
    *,
    request_id: str | None = None,
    handler_registry: CommandHandlerRegistry | None = None,
) -> MonthlyCloseVerification:
    try:
        attempt_identity = sa_inspect(attempt).identity
    except NoInspectionAvailable:
        attempt_identity = None
    attempt_id = (
        attempt_identity[0]
        if attempt_identity
        else getattr(attempt, "attempt_id", None)
    )
    if not isinstance(attempt_id, str):
        raise MonthlyCloseControlError("attempt_not_found", "执行记录不存在")
    request_id = validate_request_id(request_id or f"VERIFY:{attempt_id}")
    current_actor = await _current_admin(db, actor)
    registry = handler_registry or get_command_handler_registry()
    prelock_attempt = await db.scalar(
        select(MonthlyCloseExecutionAttempt).where(
            MonthlyCloseExecutionAttempt.attempt_id == attempt_id
        )
    )
    prelock_proposal = (
        await db.scalar(
            select(MonthlyCloseProposal).where(
                MonthlyCloseProposal.proposal_id == prelock_attempt.proposal_id,
                MonthlyCloseProposal.cycle_id == prelock_attempt.cycle_id,
            )
        )
        if prelock_attempt is not None
        else None
    )
    if prelock_proposal is not None and prelock_proposal.proposal_type in {
        "ota_reconciliation",
        "ota_appeal_adjudication",
    }:
        from app.services.monthly_close.financial_lock import (
            acquire_month_financial_lock,
        )

        original_month = await db.scalar(
            select(MonthlyCloseCycle.billing_month).where(
                MonthlyCloseCycle.cycle_id == prelock_proposal.cycle_id
            )
        )
        months = {original_month} if isinstance(original_month, str) else set()
        if prelock_proposal.proposal_type == "ota_appeal_adjudication":
            for command in prelock_proposal.canonical_payload.get("commands", []):
                later_month = (command.get("before") or {}).get(
                    "later_billing_month"
                )
                if isinstance(later_month, str):
                    months.add(later_month)
        for month in sorted(months):
            await acquire_month_financial_lock(db, month)
    locked_attempt = await db.scalar(
        select(MonthlyCloseExecutionAttempt)
        .where(MonthlyCloseExecutionAttempt.attempt_id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_attempt is None:
        raise MonthlyCloseControlError("attempt_not_found", "执行记录不存在")
    existing_verifications = list(
        await db.scalars(
            select(MonthlyCloseVerification)
            .where(MonthlyCloseVerification.attempt_id == locked_attempt.attempt_id)
            .order_by(MonthlyCloseVerification.created_at)
        )
    )
    existing_request = next(
        (
            item
            for item in existing_verifications
            if isinstance(item.checks, dict)
            and item.checks.get("request_id") == request_id
        ),
        None,
    )
    if existing_request is not None:
        return existing_request
    existing_passed = await db.scalar(
        select(MonthlyCloseVerification)
        .where(
            MonthlyCloseVerification.attempt_id == locked_attempt.attempt_id,
            MonthlyCloseVerification.status == "passed",
        )
        .order_by(MonthlyCloseVerification.created_at.desc())
        .limit(1)
    )
    if locked_attempt.status == "verified" and existing_passed is not None:
        return existing_passed
    if locked_attempt.status not in {"succeeded_unverified", "unknown"}:
        raise MonthlyCloseControlError(
            "attempt_not_verifiable", "该执行状态不能启动复核"
        )
    proposal = await db.scalar(
        select(MonthlyCloseProposal).where(
            MonthlyCloseProposal.proposal_id == locked_attempt.proposal_id,
            MonthlyCloseProposal.cycle_id == locked_attempt.cycle_id,
        )
    )
    cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == locked_attempt.cycle_id)
        .with_for_update()
    )
    if (
        proposal is None
        or cycle is None
        or locked_attempt.proposal_binding_hash
        != proposal.proposal_binding_hash
        or not proposal_binding_is_valid(proposal)
    ):
        raise MonthlyCloseControlError(
            "attempt_lineage_invalid", "执行记录的月结归属无效"
        )

    command_checks: list[dict[str, Any]] = []
    aggregate_versions: dict[str, Any] = {}
    aggregate_issues: list[MonthlyCloseIssueInstance] = []
    aggregate_kept_open_issues: list[MonthlyCloseIssueInstance] = []
    overall = "applied"
    for command in proposal.canonical_payload["commands"]:
        handler = registry.get(command["command_type"])
        if handler is None:
            normalized = await _normalize_result(
                db,
                {
                    "outcome": "inconclusive",
                    "checks": {},
                    "evidence_hash": _digest(
                        {
                            "attempt_id": locked_attempt.attempt_id,
                            "command_type": command["command_type"],
                        }
                    ),
                },
                proposal=proposal,
                command=command,
                attempt=locked_attempt,
            )
        else:
            try:
                raw_verification = await handler.verify(
                    db, cycle, proposal, command, locked_attempt
                )
            except SQLAlchemyError:
                # A database error can invalidate the transaction.  Let the
                # request boundary roll it back instead of pretending a
                # durable remediation record was written.
                raise
            except Exception:
                raw_verification = {
                    "outcome": "inconclusive",
                    "checks": {},
                    "evidence_hash": _digest(
                        {
                            "attempt_id": locked_attempt.attempt_id,
                            "command_type": command["command_type"],
                            "reason": "verifier_exception",
                        }
                    ),
                }
            normalized = await _normalize_result(
                db,
                raw_verification,
                proposal=proposal,
                command=command,
                attempt=locked_attempt,
            )
        command_checks.append(
            {
                "business_idempotency_key": command[
                    "business_idempotency_key"
                ],
                "checks": normalized["checks"],
                "command_type": command["command_type"],
                "evidence_hash": normalized["evidence_hash"],
                "outcome": normalized["outcome"],
            }
        )
        aggregate_versions.update(normalized["verified_subject_versions"])
        aggregate_issues.extend(normalized["linked_issues"])
        aggregate_kept_open_issues.extend(normalized.get("kept_open_issues", []))
        if normalized["outcome"] == "inconclusive":
            overall = "inconclusive"
        elif normalized["outcome"] == "not_applied" and overall != "inconclusive":
            overall = "not_applied"
    if len({item["outcome"] for item in command_checks}) > 1:
        overall = "inconclusive"

    previous_status = locked_attempt.status
    if overall == "applied":
        verification_status = "passed"
        failure_code = None
        next_attempt_status = "verified"
    elif overall == "not_applied" and previous_status == "unknown":
        verification_status = "passed"
        failure_code = None
        next_attempt_status = "failed_confirmed"
    else:
        verification_status = (
            "failed" if overall == "not_applied" else "inconclusive"
        )
        failure_code = (
            "EXECUTION_NOT_APPLIED"
            if overall == "not_applied"
            else "EXECUTION_VERIFICATION_INCONCLUSIVE"
        )
        next_attempt_status = "remediation_required"

    now = datetime.now(timezone.utc)
    lineage_hash = execution_lineage_hash(proposal, locked_attempt)
    verification = MonthlyCloseVerification(
        verification_id=_identifier("MCV-", 20),
        cycle_id=locked_attempt.cycle_id,
        attempt_id=locked_attempt.attempt_id,
        verification_version="monthly-close-verification-v2",
        checks={
            "commands": command_checks,
            "execution_lineage_hash": lineage_hash,
            "outcome": overall,
            "request_id": request_id,
        },
        status=verification_status,
        verified_subject_versions=aggregate_versions,
        failure_code=failure_code,
        evidence_hash=verified_execution_evidence_hash(
            attempt_id=locked_attempt.attempt_id,
            command_checks=command_checks,
            outcome=overall,
            lineage_hash=lineage_hash,
            verification_request_id=request_id,
            verified_subject_versions=aggregate_versions,
        ),
        verified_at=now,
    )
    db.add(verification)
    await db.flush()
    if overall == "applied" and proposal.proposal_type == "finalize_monthly_close":
        snapshot = (
            dict(cycle.final_snapshot)
            if isinstance(cycle.final_snapshot, dict)
            else {}
        )
        if (
            snapshot.get("executing_attempt_id") != locked_attempt.attempt_id
            or snapshot.get("executing_proposal_id") != proposal.proposal_id
        ):
            raise MonthlyCloseControlError(
                "finalization_verification_lineage_invalid",
                "关账完成记录与本次确定性复核不一致",
            )
        snapshot["verified_finalization"] = {
            "attempt_id": locked_attempt.attempt_id,
            "proposal_id": proposal.proposal_id,
            "verification_id": verification.verification_id,
            "verification_evidence_hash": verification.evidence_hash,
        }
        cycle.final_snapshot = snapshot
    locked_attempt.status = next_attempt_status
    if previous_status == "unknown" and next_attempt_status in {
        "verified",
        "failed_confirmed",
    }:
        locked_attempt.resolved_from_unknown_at = now
        locked_attempt.resolution_evidence_refs = [verification.verification_id]

    if overall == "applied":
        await _resolve_verified_issues(
            db,
            verification,
            list({issue.issue_id: issue for issue in aggregate_issues}.values()),
        )
        await _keep_verified_issues_open(
            verification,
            list({issue.issue_id: issue for issue in aggregate_kept_open_issues}.values()),
        )
        if proposal.proposal_type == "ota_reconciliation":
            from app.models.recon import ReconDiff, ReconDiffStatus
            from app.services.billing_recon.summary import review_batch_tx

            batch_ids = {
                str((command.get("before") or {}).get("batch_id", ""))
                for command in proposal.canonical_payload["commands"]
            }
            if len(batch_ids) == 1 and "" not in batch_ids:
                batch_id = next(iter(batch_ids))
                pending_diff = await db.scalar(
                    select(ReconDiff.diff_id)
                    .where(
                        ReconDiff.batch_id == batch_id,
                        ReconDiff.status == ReconDiffStatus.pending,
                    )
                    .limit(1)
                )
                if pending_diff is None:
                    # Admin approval plus a passed deterministic verification
                    # is stronger evidence than the legacy summary checkbox.
                    # Close that summary gate only after every diff is decided.
                    await review_batch_tx(db, batch_id, current_actor.user_id)
    if verification_status != "passed":
        await _ensure_failure_remediation(db, locked_attempt, verification)

    await log_action_tx(
        db,
        current_actor.user_id,
        "monthly_close.execution.verify",
        "monthly_close_execution_attempt",
        locked_attempt.attempt_id,
        after_data={
            "attempt_id": locked_attempt.attempt_id,
            "attempt_status": next_attempt_status,
            "outcome": overall,
            "request_id": request_id,
            "verification_id": verification.verification_id,
            "verification_status": verification_status,
        },
    )
    await db.commit()
    await db.refresh(verification)
    return verification
