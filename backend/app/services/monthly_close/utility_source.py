"""Thin monthly-close adapter around the deterministic utility engine."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from decimal import Decimal
from hashlib import sha256
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import undefer

from app.models.audit_log import AuditLog
from app.models.monthly_close import MonthlyCloseCycle, MonthlyCloseDocument
from app.models.utility_recon import (
    UtilityReconBatch,
    UtilityReconRow,
    UtilityReconSuggestion,
    UtilityReconUpload,
)
from app.services.audit import log_action_tx
from app.services.monthly_close.source_contract import (
    SourceAdapterCommand,
    SourceAdapterIssue,
    SourceAdapterResult,
    bind_source_command_issues as _bind_source_command_issues,
    command_without_issue_refs as _command_without_issue_refs,
    find_source_command_audit as _find_source_command_audit,
    source_evidence_is_current as _source_evidence_is_current,
    source_digest as _source_digest,
    verify_source_command as _verify_source_command,
)
from app.services.monthly_close.control import (
    MonthlyCloseControlError,
    ProposalFreshnessContext,
    create_proposal,
)
from app.services.monthly_close.documents import MonthlyCloseDocumentError
from app.services.monthly_close.financial_lock import acquire_month_financial_lock
from app.services.monthly_close.layout_memory import (
    find_remembered_mapping,
    remember_document_mapping,
)
from app.services.utility_recon.ai_mapping import UtilityColumnMapping
from app.services.utility_recon.contracts import WorkbookInput
from app.services.utility_recon.run import (
    UtilityUploadPlan,
    canonical_utility_row_fact,
    canonical_utility_suggestion_fact,
    persisted_batch_fact,
    plan_upload,
    planned_batch_fact,
    run_upload_tx,
)
from app.services.utility_recon.workbook import inspect_workbooks_with_ai


_RULESET_VERSION = "utility-reconciliation-v1"
_CALCULATION_VERSION = "utility-normalize-match-v1"
_SOURCE_TYPES = ("utility_receipt", "utility_expense")


def utility_contract_versions() -> tuple[str, str]:
    """Expose the current deterministic contract without copying version strings."""
    return _RULESET_VERSION, _CALCULATION_VERSION


async def _documents_and_plan(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
) -> tuple[
    list[MonthlyCloseDocument],
    list[WorkbookInput],
    UtilityUploadPlan | None,
    list[SourceAdapterIssue],
]:
    documents = list(
        await db.scalars(
            select(MonthlyCloseDocument)
            .options(undefer(MonthlyCloseDocument.content))
            .where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.source_type.in_(_SOURCE_TYPES),
                MonthlyCloseDocument.is_active.is_(True),
            )
            .order_by(MonthlyCloseDocument.source_type, MonthlyCloseDocument.document_id)
        )
    )
    issues: list[SourceAdapterIssue] = []
    by_type: dict[str, list[MonthlyCloseDocument]] = {
        source_type: [item for item in documents if item.source_type == source_type]
        for source_type in _SOURCE_TYPES
    }
    if any(len(by_type[source_type]) != 1 for source_type in _SOURCE_TYPES):
        for source_type in _SOURCE_TYPES:
            if len(by_type[source_type]) != 1:
                issues.append(
                    SourceAdapterIssue(
                        issue_key=f"utility-documents:{source_type}",
                        code="utility_document_count_invalid",
                        message="水电来源需要恰好一份有效原表。",
                        evidence={
                            "active_document_ids": sorted(
                                item.document_id for item in by_type[source_type]
                            ),
                            "source_type": source_type,
                        },
                    )
                )
        return documents, [], None, issues

    ordered = [by_type[source_type][0] for source_type in _SOURCE_TYPES]
    files = [WorkbookInput(item.filename, item.content) for item in ordered]
    mappings: list[UtilityColumnMapping | None] = []
    for document in ordered:
        stored = (document.metadata_ or {}).get("utility_mapping")
        remembered = None
        if not isinstance(stored, dict):
            remembered = await find_remembered_mapping(
                db,
                source_type=document.source_type,
                data=document.content,
                filename=document.filename,
            )
        candidate = stored if isinstance(stored, dict) else remembered
        mappings.append(
            UtilityColumnMapping.model_validate(candidate)
            if candidate is not None
            else None
        )
    try:
        preflight = await inspect_workbooks_with_ai(
            files,
            mappings=mappings,
            target_month=cycle.billing_month,
        )
        expected_roles = {
            "utility_receipt": "receipt",
            "utility_expense": "expense",
        }
        for document, inspected, mapping in zip(
            ordered, preflight.files, mappings, strict=True
        ):
            if inspected.role != expected_roles[document.source_type]:
                raise MonthlyCloseDocumentError(
                    "utility_role_mismatch", "水电资料类型与表格内容不一致", 422
                )
            if inspected.mapping_status == "mapped_by_ai" and mapping is None:
                issues.append(
                    SourceAdapterIssue(
                        issue_key=f"utility-mapping:{document.document_id}",
                        code="utility_mapping_confirmation_required",
                        message="检测到的字段需要人工确认后才能形成财务方案。",
                        evidence={
                            "document_id": document.document_id,
                            "sha256": document.sha256,
                        },
                    )
                )
        if any(
            item.code == "utility_mapping_confirmation_required" for item in issues
        ):
            return ordered, files, None, issues
        plan = await plan_upload(files, preflight=preflight)
    except (MonthlyCloseDocumentError, ValueError) as exc:
        issues.append(
            SourceAdapterIssue(
                issue_key="utility-plan",
                code=getattr(exc, "code", "utility_plan_invalid"),
                message="水电原表还不能形成确定性对账方案。",
                evidence={
                    "document_ids": [item.document_id for item in ordered],
                    "error": str(exc),
                },
            )
        )
        return ordered, files, None, issues
    return ordered, files, plan, issues


def _result_after(cycle: MonthlyCloseCycle, plan: UtilityUploadPlan) -> dict[str, Any]:
    result = next(item for item in plan.results if item.month == cycle.billing_month)
    scoped_plan = _plan_for_cycle(plan, cycle.billing_month)
    rows = sorted(
        (canonical_utility_row_fact(item) for item in result.rows),
        key=lambda item: (
            item["side"],
            item["source_filename"],
            item["source_sheet"],
            item["source_row_number"],
        ),
    )
    suggestions = sorted(
        (canonical_utility_suggestion_fact(item) for item in result.suggestions),
        key=lambda item: (item["kind"], item["related_row_refs"]),
    )
    return {
        "batch": planned_batch_fact(result),
        "billing_month": cycle.billing_month,
        "pair_fingerprint": plan.fingerprint_key,
        "raw_summary": planned_batch_fact(result)["raw_summary"],
        "upload": {
            "common_months": [cycle.billing_month],
            "expense_months": next(
                item.months
                for item in scoped_plan.preflight.files
                if item.role == "expense"
            ),
            "file_fingerprints": {
                **scoped_plan.fingerprints,
                "pair": scoped_plan.fingerprint_key,
            },
            "preflight_stats": {
                "excluded": scoped_plan.excluded_count,
                "unparseable": scoped_plan.unparseable_count,
            },
            "receipt_months": next(
                item.months
                for item in scoped_plan.preflight.files
                if item.role == "receipt"
            ),
            "role_mapping": {
                item.filename: item.role for item in scoped_plan.preflight.files
            },
            "status": "completed",
        },
        "rows": rows,
        "suggestions": suggestions,
    }


def _effective_mapping(inspected: Any) -> UtilityColumnMapping:
    table = inspected.sheets[0]
    return UtilityColumnMapping(
        role=inspected.role,
        sheet=table.sheet,
        header_row=table.header_row - 1,
        columns=table.columns,
    )


def _plan_for_cycle(plan: UtilityUploadPlan, billing_month: str) -> UtilityUploadPlan:
    """Narrow persistence to the approved month without rerunning any money logic."""
    return replace(
        plan,
        preflight=replace(plan.preflight, common_months=[billing_month]),
        fingerprints={**plan.fingerprints, "source_pair": plan.fingerprint_key},
        fingerprint_key=sha256(
            f"{plan.fingerprint_key}:{billing_month}".encode("utf-8")
        ).hexdigest(),
        normalized=tuple(item for item in plan.normalized if item.month == billing_month),
        results=tuple(item for item in plan.results if item.month == billing_month),
    )


async def adapt_utility_source(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
) -> SourceAdapterResult:
    documents, _files, plan, issues = await _documents_and_plan(db, cycle)
    inspected_by_role = (
        {item.role: item for item in plan.preflight.files} if plan is not None else {}
    )
    document_refs = tuple(
        {
            "document_id": item.document_id,
            "content_sha256": sha256(item.content).hexdigest(),
            "kind": "document",
            "mapping_hash": _source_digest(
                _effective_mapping(inspected_by_role[
                    "receipt" if item.source_type == "utility_receipt" else "expense"
                ]).model_dump(mode="json")
                if inspected_by_role
                else (item.metadata_ or {}).get("utility_mapping") or {}
            ),
            "sha256": item.sha256,
            "source_type": item.source_type,
        }
        for item in documents
    )
    commands: tuple[SourceAdapterCommand, ...] = ()
    if plan is not None:
        result = next(
            (item for item in plan.results if item.month == cycle.billing_month), None
        )
        if result is None:
            issues.append(
                SourceAdapterIssue(
                    issue_key=f"utility-month:{cycle.billing_month}",
                    code="utility_month_not_found",
                    message="两份水电原表没有本月共同数据。",
                    evidence={
                        "billing_month": cycle.billing_month,
                        "common_months": plan.preflight.common_months,
                        "expense_only_months": plan.preflight.expense_only_months,
                        "receipt_only_months": plan.preflight.receipt_only_months,
                    },
                )
            )
        else:
            after = _result_after(cycle, plan)
            # The month is the economic event identity.  The exact source-pair
            # fingerprint remains immutable evidence and command data, so a
            # replacement pair changes approved bytes without masquerading as
            # a second utility reconciliation for the same close.
            subject_id = f"utility:{cycle.billing_month}"
            command = SourceAdapterCommand(
                command_type="utility_reconciliation",
                subject_id=subject_id,
                before={
                    "billing_month": cycle.billing_month,
                    "cycle_id": cycle.cycle_id,
                    "linked_batch_ids": sorted(
                        {
                            item.engine_id
                            for item in documents
                            if item.engine_type == "utility_recon" and item.engine_id
                        }
                    ),
                },
                after=after,
                amount_impact=Decimal("0.00"),
                evidence_refs=[
                    {
                        "control_version": cycle.control_version,
                        "cycle_id": cycle.cycle_id,
                        "kind": "cycle",
                    },
                    {
                        "calculation_version": _CALCULATION_VERSION,
                        "kind": "ruleset",
                        "ruleset_version": _RULESET_VERSION,
                    },
                    *document_refs,
                    {
                        "kind": "utility_plan",
                        "pair_fingerprint": plan.fingerprint_key,
                        "plan_hash": _source_digest(after),
                    },
                ],
                business_idempotency_key=subject_id,
            )
            commands = (command,)
            issues.append(
                SourceAdapterIssue(
                    issue_key=subject_id,
                    code="utility_import_required",
                    message="水电原表已有确定性批次待管理员批准保存。",
                    evidence={
                        "pair_fingerprint": plan.fingerprint_key,
                        "plan_hash": _source_digest(after),
                    },
                    command_key=subject_id,
                )
            )
            for index, suggestion in enumerate(after["suggestions"]):
                issues.append(
                    SourceAdapterIssue(
                        issue_key=f"{subject_id}:suggestion:{index}",
                        code="utility_manual_allocation_required",
                        message="水电差异或分摊需要人工明确确认。",
                        evidence={
                            "kind": suggestion["kind"],
                            "related_row_refs": suggestion["related_row_refs"],
                        },
                    )
                )
            if plan.excluded_count or plan.unparseable_count:
                issues.append(
                    SourceAdapterIssue(
                        issue_key=f"{subject_id}:unresolved-rows",
                        code="utility_rows_require_confirmation",
                        message="水电原表仍有排除或无法解析的行。",
                        evidence={
                            "excluded_count": plan.excluded_count,
                            "unparseable_count": plan.unparseable_count,
                        },
                    )
                )
            if plan.preflight.receipt_only_months or plan.preflight.expense_only_months:
                issues.append(
                    SourceAdapterIssue(
                        issue_key=f"{subject_id}:cross-month",
                        code="utility_cross_month_confirmation_required",
                        message="两份水电原表包含不一致月份，需要人工确认。",
                        evidence={
                            "expense_only_months": plan.preflight.expense_only_months,
                            "receipt_only_months": plan.preflight.receipt_only_months,
                        },
                    )
                )
    has_uncommanded = any(item.command_key is None for item in issues)
    if not documents:
        state = "missing"
    elif has_uncommanded:
        state = "needs_confirmation"
    elif commands:
        state = "ready"
    else:
        state = "completed"
    return SourceAdapterResult(
        source_type="utilities",
        state=state,
        issues=tuple(sorted(issues, key=lambda item: item.issue_key)),
        commands=commands,
        verification_query={
            "billing_month": cycle.billing_month,
            "cycle_id": cycle.cycle_id,
            "document_ids": [item.document_id for item in documents],
            "pair_fingerprint": plan.fingerprint_key if plan else None,
        },
        evidence_refs=(
            {
                "calculation_version": _CALCULATION_VERSION,
                "kind": "ruleset",
                "ruleset_version": _RULESET_VERSION,
            },
            {
                "control_version": cycle.control_version,
                "cycle_id": cycle.cycle_id,
                "kind": "cycle",
            },
            *document_refs,
        ),
    )


def _context(
    cycle: MonthlyCloseCycle, adapter: SourceAdapterResult
) -> ProposalFreshnessContext:
    commands = [_command_without_issue_refs(item) for item in adapter.commands]
    material = {
        "commands": commands,
        "evidence_refs": list(adapter.evidence_refs),
        "issues": [item.model_dump(mode="json") for item in adapter.issues],
        "verification_query": adapter.verification_query,
    }
    return ProposalFreshnessContext(
        evidence_hash=_source_digest(material),
        subject_versions={
            item["subject_id"]: _source_digest(
                {"after": item["after"], "evidence_refs": item["evidence_refs"]}
            )
            for item in commands
        },
        active_input_set_hash=_source_digest(list(adapter.evidence_refs)),
        mapping_versions={
            item["document_id"]: item["mapping_hash"]
            for item in adapter.evidence_refs
            if item.get("kind") == "document"
        },
        ruleset_version=_RULESET_VERSION,
        calculation_version=_CALCULATION_VERSION,
        configuration_snapshot_hash=_source_digest(
            {
                "billing_month": cycle.billing_month,
                "ruleset_version": _RULESET_VERSION,
            }
        ),
        control_version=cycle.control_version,
    )


async def load_utility_context(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    _commands: Sequence[dict[str, Any]],
) -> ProposalFreshnessContext:
    return _context(cycle, await adapt_utility_source(db, cycle))


async def build_utility_proposal(
    db: AsyncSession,
    cycle_id: str,
    actor: Any,
    *,
    request_id: str | None = None,
):
    cycle = await db.get(MonthlyCloseCycle, cycle_id)
    if cycle is None:
        raise MonthlyCloseControlError("cycle_not_found", "月结周期不存在")
    adapter = await adapt_utility_source(db, cycle)
    if not adapter.commands:
        raise MonthlyCloseControlError(
            "utility_no_safe_commands", "水电来源没有可执行的确定性变更"
        )
    issue_rows, commands = await _bind_source_command_issues(
        db, cycle, adapter, adapter_type="utility_reconciliation"
    )
    context = _context(cycle, adapter)
    proposal = await create_proposal(
        db,
        cycle,
        actor,
        commands,
        evidence_hash=context.evidence_hash,
        request_id=request_id,
        proposal_type="utility_reconciliation",
        evidence_refs=adapter.evidence_refs,
        subject_versions=context.subject_versions,
        active_input_set_hash=context.active_input_set_hash,
        mapping_versions=context.mapping_versions,
        ruleset_version=context.ruleset_version,
        calculation_version=context.calculation_version,
        configuration_snapshot_hash=context.configuration_snapshot_hash,
        server_owned_commands=True,
        semantic_source_submission=True,
        impact_snapshot={
            "change_count": len(commands),
            "required_approver": "管理员",
            "risk": "将保存精确原表生成的水电批次；差异仍需另行人工确认",
            "total_amount": "0.00",
            "verification": (
                "执行后重读精确文件、批次汇总、逐行事实、建议和审计。"
            ),
        },
    )
    for issue in issue_rows.values():
        issue.status = "proposed_fix"
        issue.resolution_proposal_id = proposal.proposal_id
    await db.commit()
    await db.refresh(proposal)
    return proposal


async def execute_utility_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    actor: Any,
    request_id: str,
) -> dict[str, Any]:
    await acquire_month_financial_lock(db, cycle.billing_month)
    locked_cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if locked_cycle is None or locked_cycle.write_control_owner != "assistant":
        raise MonthlyCloseControlError(
            "utility_control_owner_changed", "水电写入控制权已经变化"
        )
    current_adapter = await adapt_utility_source(db, locked_cycle)
    current = next(
        (
            item
            for item in current_adapter.commands
            if item.business_idempotency_key
            == command.get("business_idempotency_key")
        ),
        None,
    )
    if current is None or _command_without_issue_refs(
        current
    ) != _command_without_issue_refs(command):
        raise MonthlyCloseControlError(
            "utility_command_stale", "水电原件或映射已经变化，请重新生成方案"
        )
    documents, files, plan, issues = await _documents_and_plan(db, locked_cycle)
    if plan is None or issues:
        raise MonthlyCloseControlError(
            "utility_command_stale", "水电原件已不能形成批准时的确定性方案"
        )
    scoped_plan = _plan_for_cycle(plan, locked_cycle.billing_month)
    if len(scoped_plan.results) != 1:
        raise MonthlyCloseControlError(
            "utility_command_stale", "批准月份不再是水电原表的唯一执行范围"
        )
    batches = await run_upload_tx(
        db,
        files,
        actor.user_id,
        preflight=scoped_plan.preflight,
        plan=scoped_plan,
    )
    batch = next(
        (item for item in batches if item.month == locked_cycle.billing_month), None
    )
    if batch is None:
        raise MonthlyCloseControlError(
            "utility_effect_mismatch", "批准月份的水电批次未生成"
        )
    for document, inspected in zip(documents, plan.preflight.files, strict=True):
        mapping = _effective_mapping(inspected)
        document.engine_type = "utility_recon"
        document.engine_id = batch.batch_id
        document.processing_status = "processed"
        document.processing_error = None
        document.metadata_ = {
            **(document.metadata_ or {}),
            "billing_month": locked_cycle.billing_month,
            "utility_batch_id": batch.batch_id,
            "utility_mapping": mapping.model_dump(mode="json"),
        }
        remember_document_mapping(
            document,
            data=document.content,
            filename=document.filename,
            mapping=mapping,
        )
    await log_action_tx(
        db,
        actor.user_id,
        "monthly_close.utility.execute",
        "monthly_close",
        locked_cycle.cycle_id,
        before_data=command["before"],
        after_data={
            "approved_after": command["after"],
            "batch_id": batch.batch_id,
            "business_idempotency_key": command["business_idempotency_key"],
            "proposal_id": proposal.proposal_id,
            "request_id": request_id,
        },
    )
    await db.flush()
    audit_id = await db.scalar(
        select(func.max(AuditLog.log_id)).where(
            AuditLog.action == "monthly_close.utility.execute",
            AuditLog.resource_id == locked_cycle.cycle_id,
            AuditLog.operator_id == actor.user_id,
        )
    )
    execution_result = _utility_execution_audit_identity(command)["result"]
    return {
        "result": execution_result,
        "audit_refs": [str(audit_id)] if audit_id is not None else [],
        "execution_audits": (
            [
                {
                    "audit_ref": str(audit_id),
                    **_utility_execution_audit_identity(command),
                }
            ]
            if audit_id is not None
            else []
        ),
    }


def _utility_execution_audit_identity(command: dict[str, Any]) -> dict[str, Any]:
    return {
        "action": "monthly_close.utility.execute",
        "resource_type": "monthly_close",
        "resource_id": command["before"]["cycle_id"],
        "result": {
            "billing_month": command["after"]["billing_month"],
            "pair_fingerprint": command["after"]["pair_fingerprint"],
        },
    }


async def _persisted_after(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    attempt: Any,
) -> dict[str, Any]:
    audit = await _find_source_command_audit(
        db,
        proposal,
        command,
        attempt,
        audit_action="monthly_close.utility.execute",
        audit_resource_id=cycle.cycle_id,
    )
    batch_id = (audit.after_data or {}).get("batch_id") if audit else None
    batch = await db.get(UtilityReconBatch, batch_id) if batch_id else None
    if batch is None:
        return {}
    upload = await db.get(UtilityReconUpload, batch.upload_id)
    if (
        upload is None
        or upload.common_months != [cycle.billing_month]
        or (upload.file_fingerprints or {}).get("source_pair")
        != command["after"]["pair_fingerprint"]
    ):
        return {}
    linked_document_ids = set(
        await db.scalars(
            select(MonthlyCloseDocument.document_id).where(
                MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                MonthlyCloseDocument.engine_type == "utility_recon",
                MonthlyCloseDocument.engine_id == batch.batch_id,
                MonthlyCloseDocument.is_active.is_(True),
            )
        )
    )
    approved_document_ids = {
        item["document_id"]
        for item in proposal.evidence_refs
        if item.get("kind") == "document"
    }
    if linked_document_ids != approved_document_ids:
        return {}
    rows = list(
        await db.scalars(
            select(UtilityReconRow)
            .where(UtilityReconRow.batch_id == batch.batch_id)
            .order_by(
                UtilityReconRow.side,
                UtilityReconRow.source_filename,
                UtilityReconRow.source_sheet,
                UtilityReconRow.source_row_number,
            )
        )
    )
    suggestions = list(
        await db.scalars(
            select(UtilityReconSuggestion)
            .where(UtilityReconSuggestion.batch_id == batch.batch_id)
            .order_by(UtilityReconSuggestion.kind, UtilityReconSuggestion.suggestion_id)
        )
    )
    row_ref_by_id = {
        stored.row_id: (
            f"{stored.source_filename}:{stored.source_sheet}:"
            f"{stored.source_row_number}"
        )
        for stored in rows
    }
    suggestion_facts = [
        canonical_utility_suggestion_fact(
            item,
            row_reference=lambda row_id: row_ref_by_id.get(row_id, row_id),
        )
        for item in suggestions
    ]
    return {
        "batch": persisted_batch_fact(batch),
        "billing_month": batch.month,
        "pair_fingerprint": (upload.file_fingerprints or {}).get("source_pair"),
        "raw_summary": batch.raw_summary,
        "upload": {
            "common_months": upload.common_months,
            "expense_months": upload.expense_months,
            "file_fingerprints": upload.file_fingerprints,
            "preflight_stats": upload.preflight_stats,
            "receipt_months": upload.receipt_months,
            "role_mapping": upload.role_mapping,
            "status": upload.status,
        },
        "rows": [canonical_utility_row_fact(item) for item in rows],
        "suggestions": sorted(
            suggestion_facts,
            key=lambda item: (item["kind"], item["related_row_refs"]),
        ),
    }


async def verify_utility_command(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: Any,
    command: dict[str, Any],
    attempt: Any,
) -> dict[str, Any]:
    actual = await _persisted_after(db, cycle, proposal, command, attempt)
    current_adapter = await adapt_utility_source(db, cycle)
    current_command = next(
        (
            item
            for item in current_adapter.commands
            if item.subject_id == command["subject_id"]
        ),
        None,
    )
    evidence_current = _source_evidence_is_current(
        cycle=cycle,
        proposal=proposal,
        current_adapter=current_adapter,
        ruleset_version=_RULESET_VERSION,
        calculation_version=_CALCULATION_VERSION,
    ) and (
        current_command is not None
        and current_command.after == command["after"]
    )
    return await _verify_source_command(
        db,
        proposal,
        command,
        attempt,
        actual=actual,
        expected_audit_identity=_utility_execution_audit_identity,
        evidence_current=evidence_current,
    )
