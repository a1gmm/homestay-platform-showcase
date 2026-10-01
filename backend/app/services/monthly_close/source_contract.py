"""Shared contract and verification primitives for monthly-close sources."""

from __future__ import annotations

from collections import defaultdict
from datetime import date, datetime, timezone
from decimal import Decimal
import hashlib
import json
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog
from app.models.monthly_close import MonthlyCloseCycle
from app.models.monthly_close_control import (
    MonthlyCloseIssueInstance,
    MonthlyCloseOutbox,
)
from app.services.monthly_close.control import (
    CanonicalCommand,
    ExecutionAuditIdentity,
    authoritative_execution_evidence_is_valid,
)


class SourceAdapterCommand(CanonicalCommand):
    """One canonical command emitted by a non-OTA source adapter."""


class SourceAdapterIssue(BaseModel):
    """Stable source issue; issue data is evidence, never executable prose."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    issue_key: str
    code: str
    message: str
    evidence: dict[str, Any]
    command_key: str | None = None


class SourceAdapterResult(BaseModel):
    """Shared typed shape for every independently progressing source."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    source_type: str
    state: Literal["missing", "needs_confirmation", "ready", "completed", "blocked"]
    issues: tuple[SourceAdapterIssue, ...] = ()
    commands: tuple[SourceAdapterCommand, ...] = ()
    verification_query: dict[str, Any]
    evidence_refs: tuple[dict[str, Any], ...]


def source_json_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise ValueError("source evidence cannot contain floating point values")
    if isinstance(value, Decimal):
        return format(value.quantize(Decimal("0.01")), ".2f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if hasattr(value, "value"):
        return source_json_value(value.value)
    if isinstance(value, dict):
        return {
            str(key): source_json_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [source_json_value(item) for item in value]
    raise ValueError(f"unsupported source evidence type: {type(value).__name__}")


def source_digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(
            source_json_value(value),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def command_without_issue_refs(
    command: SourceAdapterCommand | dict[str, Any],
) -> dict[str, Any]:
    parsed = (
        command
        if isinstance(command, SourceAdapterCommand)
        else SourceAdapterCommand.model_validate(command)
    )
    value = parsed.model_dump(mode="python")
    value["amount_impact"] = format(parsed.amount_impact, ".2f")
    value["evidence_refs"] = sorted(
        [
            source_json_value(ref)
            for ref in value["evidence_refs"]
            if ref.get("kind") != "issue"
        ],
        key=lambda item: json.dumps(
            item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ),
    )
    return source_json_value(value)


async def bind_source_command_issues(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    adapter: SourceAdapterResult,
    *,
    adapter_type: str,
) -> tuple[dict[str, MonthlyCloseIssueInstance], list[dict[str, Any]]]:
    issue_rows: dict[str, MonthlyCloseIssueInstance] = {}
    refs_by_command: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in adapter.issues:
        if item.command_key is None:
            continue
        evidence_hash = source_digest(
            {"code": item.code, "evidence": item.evidence, "issue_key": item.issue_key}
        )
        issue = await db.scalar(
            select(MonthlyCloseIssueInstance)
            .where(
                MonthlyCloseIssueInstance.cycle_id == cycle.cycle_id,
                MonthlyCloseIssueInstance.adapter_type == adapter_type,
                MonthlyCloseIssueInstance.source_subject_id == item.command_key,
                MonthlyCloseIssueInstance.issue_key == item.issue_key,
            )
            .with_for_update()
        )
        if issue is None:
            issue = MonthlyCloseIssueInstance(
                issue_id=f"MCI-{uuid4().hex[:20].upper()}",
                cycle_id=cycle.cycle_id,
                adapter_type=adapter_type,
                source_subject_id=item.command_key,
                issue_key=item.issue_key,
                status="open",
                last_seen_evidence_hash=evidence_hash,
            )
            db.add(issue)
            await db.flush()
        else:
            previous_evidence_hash = issue.last_seen_evidence_hash
            previous_status = issue.status
            if (
                issue.status in {"resolved", "cancelled"}
                and previous_evidence_hash != evidence_hash
            ):
                issue.status = "reopened"
                issue.reopened_at = datetime.now(timezone.utc)
                history_key = (
                    f"issue-history:{issue.issue_id}:"
                    f"{previous_evidence_hash[:16]}:{evidence_hash[:16]}"
                )
                existing_history = await db.scalar(
                    select(MonthlyCloseOutbox.outbox_id).where(
                        MonthlyCloseOutbox.dedupe_key == history_key
                    )
                )
                if existing_history is None:
                    db.add(
                        MonthlyCloseOutbox(
                            outbox_id=f"MCO-{uuid4().hex[:20].upper()}",
                            cycle_id=cycle.cycle_id,
                            topic="monthly_close.issue_history",
                            payload={
                                "issue_id": issue.issue_id,
                                "kind": "evidence_changed_reopen",
                                "previous_evidence_hash": previous_evidence_hash,
                                "current_evidence_hash": evidence_hash,
                                "previous_status": previous_status,
                                "resolution_note": issue.resolution_note,
                                "resolution_proposal_id": issue.resolution_proposal_id,
                                "verification_id": issue.verification_id,
                            },
                            dedupe_key=history_key,
                        )
                    )
            issue.last_seen_evidence_hash = evidence_hash
        issue_rows[issue.issue_id] = issue
        refs_by_command[item.command_key].append(
            {
                "evidence_hash": evidence_hash,
                "issue_id": issue.issue_id,
                "kind": "issue",
            }
        )
    commands: list[dict[str, Any]] = []
    for item in adapter.commands:
        raw = item.model_dump(mode="python")
        raw["evidence_refs"] = [
            *raw["evidence_refs"],
            *refs_by_command[item.business_idempotency_key],
        ]
        commands.append(raw)
    return issue_rows, commands


def _command_result_for_attempt(
    command: dict[str, Any], attempt: Any
) -> dict[str, Any] | None:
    results = attempt.command_results if isinstance(attempt.command_results, list) else []
    return next(
        (
            item
            for item in results
            if isinstance(item, dict)
            and item.get("business_idempotency_key")
            == command.get("business_idempotency_key")
            and item.get("command_type") == command.get("command_type")
        ),
        None,
    )


async def find_source_command_audit(
    db: AsyncSession,
    proposal: Any,
    command: dict[str, Any],
    attempt: Any,
    *,
    audit_action: str,
    audit_resource_id: str,
) -> AuditLog | None:
    result = _command_result_for_attempt(command, attempt)
    refs = result.get("audit_refs") if isinstance(result, dict) else None
    if not isinstance(refs, list) or len(refs) != 1 or refs[0] not in attempt.audit_refs:
        return None
    try:
        audit = await db.get(AuditLog, int(refs[0]))
    except (TypeError, ValueError):
        return None
    if (
        audit is None
        or audit.action != audit_action
        or audit.resource_id != audit_resource_id
        or audit.operator_id != attempt.actor_id
    ):
        return None
    return audit


def source_evidence_is_current(
    *,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    current_adapter: SourceAdapterResult,
    ruleset_version: str,
    calculation_version: str,
) -> bool:
    """Compare proposal evidence with a freshly rebuilt source projection."""
    def ordered(values: Any) -> list[Any]:
        return sorted(
            (source_json_value(item) for item in values),
            key=lambda item: json.dumps(
                item, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ),
        )

    return (
        cycle.control_version
        == (proposal.validation_snapshot or {}).get("control_version")
        and proposal.ruleset_version == ruleset_version
        and proposal.calculation_version == calculation_version
        and ordered(proposal.evidence_refs) == ordered(current_adapter.evidence_refs)
    )


async def verify_source_command(
    db: AsyncSession,
    proposal: Any,
    command: dict[str, Any],
    attempt: Any,
    *,
    actual: dict[str, Any],
    expected_audit_identity: ExecutionAuditIdentity,
    evidence_current: bool,
) -> dict[str, Any]:
    audit_present = await authoritative_execution_evidence_is_valid(
        db,
        proposal=proposal,
        attempt=attempt,
        expected_audit_identity=expected_audit_identity,
        lock=True,
    )
    issue_results: list[dict[str, Any]] = []
    for ref in command.get("evidence_refs", []):
        if ref.get("kind") != "issue":
            continue
        issue = await db.get(MonthlyCloseIssueInstance, ref["issue_id"])
        issue_results.append(
            {
                "actual_evidence_hash": (
                    issue.last_seen_evidence_hash if issue else "0" * 64
                ),
                "approved_evidence_hash": ref["evidence_hash"],
                "issue_id": ref["issue_id"],
                "resolution": "resolve",
            }
        )
    applied = actual == command["after"] and audit_present and evidence_current
    outcome = "applied" if applied else "not_applied" if not audit_present else "inconclusive"
    expected_version = proposal.subject_versions.get(command["subject_id"])
    return {
        "outcome": outcome,
        "business_idempotency_key": command["business_idempotency_key"],
        "subject": {
            "subject_id": command["subject_id"],
            "expected_version": expected_version,
            "actual_version": expected_version if evidence_current else None,
        },
        "target": {
            "expected_before": command["before"],
            "expected_after": command["after"],
            "actual": actual,
        },
        "amount": {
            "expected": command["amount_impact"],
            "actual": command["amount_impact"] if applied else "0.00",
        },
        "audit": {
            "present": audit_present,
            "request_id": attempt.request_id,
            "business_idempotency_key": command["business_idempotency_key"],
            "expected_before": command["before"],
            "expected_after": command["after"],
        },
        "idempotency": {
            "present": audit_present,
            "request_id": attempt.request_id,
            "business_idempotency_key": command["business_idempotency_key"],
        },
        "evidence": {
            "approved_evidence_hash": proposal.evidence_hash,
            "verification_evidence_hash": source_digest(
                {
                    "actual": actual,
                    "attempt_id": attempt.attempt_id,
                    "audit_present": audit_present,
                    "evidence_current": evidence_current,
                    "issues": issue_results,
                    "outcome": outcome,
                }
            ),
        },
        "issues": issue_results,
    }
