"""Deterministic evidence snapshots for the fixed monthly-close checklist."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
import hashlib
import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monthly_close import (
    MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES,
    MONTHLY_CLOSE_SOURCE_TYPES,
    MONTHLY_CLOSE_STEP_KEYS,
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseInboxItem,
    MonthlyCloseSourceRequirement,
    uses_legacy_utility_contract,
)
from app.services.monthly_close.adapters import (
    exception_clearance_snapshot,
    order_integrity_snapshot,
    ota_statements_snapshot,
    owner_confirmation_snapshot,
    preflight_snapshot,
    service_fees_snapshot,
    settlement_review_snapshot,
    utilities_snapshot,
)


_SOURCE_LABELS = {
    "cleaning_statement": "保洁打扫记录",
    "linen_statement": "布草／洗涤记录",
    "utility_receipt": "历史水电资料",
    "utility_expense": "水电支出",
    "ota_statement": "OTA平台账单",
    "operating_expenses": "其他运营支出",
}


def _int(summary: dict[str, Any], key: str) -> int:
    value = summary.get(key, 0)
    return int(value) if isinstance(value, (int, float, str)) and str(value).isdigit() else 0


def step_confirmation(step_key: str, summary: dict[str, Any]) -> dict[str, Any]:
    """Build the exact business assertion and keep it inside evidence hashing."""
    if step_key == "source_collection":
        sources = summary.get("sources") if isinstance(summary.get("sources"), list) else []
        completed = sum(
            isinstance(source, dict)
            and source.get("state") in {"uploaded", "not_applicable"}
            for source in sources
        )
        return {
            "confirmation_title": f"确认 {completed} 类月结资料齐全",
            "confirmation_items": [
                f"{completed} / {len(sources) or len(MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES)} 类资料已有明确结果",
                "缺失资料 0 项",
            ],
        }
    if step_key == "order_integrity":
        orders = _int(summary, "order_count")
        rooms = _int(summary, "order_room_count")
        return {
            "confirmation_title": f"确认 {orders} 张订单完整",
            "confirmation_items": [f"{orders} 张订单", f"{rooms} 条房间明细", "阻断异常 0 项"],
        }
    if step_key == "service_fees":
        matched = _int(summary, "matched_vendor_line_count")
        total = _int(summary, "vendor_line_count")
        return {
            "confirmation_title": f"确认 {matched} 条保洁、布草明细已核对",
            "confirmation_items": [f"供应商明细 {matched} / {total} 条匹配", "服务费差异 0 项"],
        }
    if step_key == "utilities":
        closed = _int(summary, "closed_batch_count")
        imported = _int(summary, "operating_expense_import_count")
        if "processed_utility_document_count" in summary:
            processed = _int(summary, "processed_utility_document_count")
            return {
                "confirmation_title": "确认本月水电支出和运营支出已核对",
                "confirmation_items": [
                    f"已确认水电支出资料 {processed} 份",
                    f"已导入运营支出文件 {imported} 份",
                    "阻断异常 0 项",
                ],
            }
        return {
            "confirmation_title": "确认本月水电和运营支出已核对",
            "confirmation_items": [f"已关闭水电批次 {closed} 个", f"已导入运营支出文件 {imported} 份", "阻断异常 0 项"],
        }
    if step_key == "ota_statements":
        reviewed = _int(summary, "reviewed_batch_count")
        return {
            "confirmation_title": f"确认 {reviewed} 个 OTA 账单批次已复核",
            "confirmation_items": [f"已复核批次 {reviewed} 个", "开放差异 0 项"],
        }
    if step_key == "exception_clearance":
        return {
            "confirmation_title": "确认全部月结异常已处理",
            "confirmation_items": ["订单、服务费、水电和 OTA 开放异常 0 项"],
        }
    if step_key == "preflight":
        return {
            "confirmation_title": "确认月结体检通过",
            "confirmation_items": ["结算前阻断项 0 项"],
        }
    if step_key == "settlement_review":
        count = _int(summary, "settlement_count")
        settlements = summary.get("settlements") if isinstance(summary.get("settlements"), list) else []
        total = sum(
            (Decimal(str(item.get("actual_owner_amount", "0"))) for item in settlements if isinstance(item, dict)),
            Decimal("0"),
        )
        return {
            "confirmation_title": f"确认 {count} 份业主结算金额",
            "confirmation_items": [f"{count} 份结算单", f"业主合计应得 ¥{total:.2f}", "争议 0 项"],
        }
    count = _int(summary, "confirmed_count")
    return {
        "confirmation_title": f"确认 {count} 份结算均已由业主确认",
        "confirmation_items": [f"业主已确认 {count} 份", "未确认或争议 0 项"],
    }


def present_issue(issue: dict[str, Any]) -> dict[str, Any]:
    """Add a stable operator-facing explanation without removing machine fields."""
    subject = issue.get("subject")
    if not subject:
        if issue.get("order_id"):
            subject = f"订单 {issue['order_id']}"
        elif issue.get("source_type"):
            subject = _SOURCE_LABELS.get(
                str(issue["source_type"]), "月结资料"
            )
        elif issue.get("document_id"):
            subject = "相关月结文件"
        elif issue.get("settlement_id"):
            subject = "相关业主结算单"
        elif issue.get("expense_id"):
            subject = "相关运营支出"
        elif issue.get("room_id"):
            subject = f"房间 {issue['room_id']}"
        elif issue.get("batch_id"):
            subject = "相关对账批次"
        else:
            subject = "当前月结事项"

    amount = issue.get("amount")
    expected_amount = issue.get("expected_amount")
    current_amount = issue.get("current_amount")
    if expected_amount is not None and current_amount is not None:
        impact = f"当前 ¥{current_amount}，应为 ¥{expected_amount}"
    elif amount is not None:
        impact = f"涉及金额 ¥{amount}"
    else:
        impact = "阻止本步骤确认"

    action = issue.get("action")
    action_label = action.get("label") if isinstance(action, dict) else None
    next_step = (
        f"{action_label}；完成后返回本页，系统会自动重新检查。"
        if action_label
        else "按原因修正业务数据；完成后返回本页，系统会自动重新检查。"
    )
    return {
        **issue,
        "subject": subject,
        "impact": impact,
        "cause": issue.get("message") or "系统检查发现该事项尚未完成。",
        "next_step": next_step,
    }


@dataclass(frozen=True)
class StepEvidence:
    step_key: str
    evidence_hash: str
    snapshot: dict[str, Any]

    @property
    def blocking_count(self) -> int:
        return int(self.snapshot.get("blocking_count", 0))


def _canonical_hash(value: dict[str, Any]) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def compatible_confirmation_hashes(evidences, confirmations) -> dict[str, str]:
    """Project intact old confirmations onto the current fact-only hash chain.

    Never rewrite stored evidence. Only the non-blocking fee explanation and
    an already verified predecessor's hash may differ from today's snapshot.
    """
    result = {}
    predecessor_aliases = {None}
    for evidence in evidences:
        saved = confirmations.get(evidence.step_key)
        aliases = {evidence.evidence_hash}
        if saved is not None:
            matched = saved.evidence_hash == evidence.evidence_hash
            old = saved.evidence
            if not matched and isinstance(old, dict):
                def fact_snapshot(snapshot):
                    value = {**snapshot}
                    if value.get("step_key") == "preflight":
                        value["summary"] = {k: v for k, v in value.get("summary", {}).items()
                                            if k != "fee_explanations"}
                    return value
                old_facts = fact_snapshot(old)
                intact = saved.evidence_hash in {_canonical_hash(old), _canonical_hash(old_facts)}
                if intact and old.get("dependency_hash") in predecessor_aliases:
                    old_facts["dependency_hash"] = evidence.snapshot.get("dependency_hash")
                    matched = old_facts == fact_snapshot(evidence.snapshot)
            if matched:
                result[evidence.step_key] = evidence.evidence_hash
                aliases.add(saved.evidence_hash)
            else:
                result[evidence.step_key] = saved.evidence_hash
        predecessor_aliases = aliases
    return result


def build_evidence_chain(
    billing_month: str,
    raw_by_step: dict[str, dict[str, Any]],
) -> list[StepEvidence]:
    """Add dependency hashes so any upstream change invalidates later steps."""
    dependency_hash: str | None = None
    result: list[StepEvidence] = []
    for key in MONTHLY_CLOSE_STEP_KEYS:
        raw = raw_by_step[key]
        presented_issues = [present_issue(issue) for issue in raw.get("issues", [])]
        snapshot = {
            "step_key": key,
            "billing_month": billing_month,
            "dependency_hash": dependency_hash,
            "blocking_count": int(raw.get("blocking_count", 0)),
            "summary": raw.get("summary", {}),
            "issues": sorted(
                presented_issues,
                key=lambda item: (
                    str(item.get("code", "")),
                    str(item.get("resource_id", "")),
                ),
            ),
            **step_confirmation(key, raw.get("summary", {})),
        }
        # Expected fee explanations are presentation, not approval blockers.
        # Keep them visible without invalidating historical confirmations or
        # downstream steps when a normal fee is posted and its notice disappears.
        hash_snapshot = snapshot
        if key == "preflight":
            hash_snapshot = {**snapshot, "summary": {
                name: value for name, value in snapshot["summary"].items()
                if name != "fee_explanations"
            }}
        evidence_hash = _canonical_hash(hash_snapshot)
        result.append(
            StepEvidence(
                step_key=key,
                evidence_hash=evidence_hash,
                snapshot=snapshot,
            )
        )
        dependency_hash = evidence_hash
    return result


async def _source_collection_snapshot(
    db: AsyncSession, cycle: MonthlyCloseCycle,
    *, financial_case: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if financial_case is None:
        from app.services.monthly_close.financial_case_bridge import build_financial_case_snapshot
        financial_case = await build_financial_case_snapshot(db, cycle)
    requirements = list(
        (
            await db.execute(
                select(MonthlyCloseSourceRequirement).where(
                    MonthlyCloseSourceRequirement.cycle_id == cycle.cycle_id
                )
            )
        ).scalars()
    )
    requirement_by_type = {item.source_type: item for item in requirements}
    documents = list(
        (
            await db.execute(
                select(MonthlyCloseDocument).where(
                    MonthlyCloseDocument.cycle_id == cycle.cycle_id,
                    MonthlyCloseDocument.is_active.is_(True),
                )
            )
        ).scalars()
    )
    legacy_contract = uses_legacy_utility_contract(cycle, documents=documents)
    source_types = (
        MONTHLY_CLOSE_SOURCE_TYPES
        if legacy_contract
        else MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES
    )
    documents_by_type: dict[str, list[MonthlyCloseDocument]] = {}
    for document in documents:
        logical_type = (
            "utility_expense"
            if not legacy_contract and document.source_type == "utility_receipt"
            else document.source_type
        )
        documents_by_type.setdefault(logical_type, []).append(document)

    pending_inbox = list(
        (
            await db.execute(
                select(MonthlyCloseInboxItem)
                .where(
                    MonthlyCloseInboxItem.cycle_id == cycle.cycle_id,
                    MonthlyCloseInboxItem.status.notin_(("confirmed", "dismissed")),
                )
                .order_by(MonthlyCloseInboxItem.created_at, MonthlyCloseInboxItem.item_id)
            )
        ).scalars()
    )

    issues: list[dict[str, Any]] = []
    sources: list[dict[str, Any]] = []
    for source_type in source_types:
        requirement = requirement_by_type.get(source_type)
        state = requirement.state if requirement is not None else "pending"
        source_documents = sorted(
            documents_by_type.get(source_type, []), key=lambda item: item.sha256
        )
        originals = [source for source in financial_case["sources"]
                     if source_type in source["source_types"]]
        if originals and state == "pending":
            state = "uploaded"
        if source_documents and state == "pending" and not legacy_contract:
            state = "uploaded"
        if state == "pending":
            issues.append(
                {
                    "code": "source_pending",
                    "resource_id": source_type,
                    "source_type": source_type,
                    "message": "请上传资料，或填写原因标记为不适用。",
                }
            )
        elif state == "uploaded" and not source_documents and not originals:
            issues.append(
                {
                    "code": "source_document_missing",
                    "resource_id": source_type,
                    "source_type": source_type,
                    "message": "资料状态为已上传，但没有有效归档文件。",
                }
            )
        sources.append(
            {
                "source_type": source_type,
                "state": state,
                "not_applicable_reason": (
                    requirement.not_applicable_reason if requirement is not None else None
                ),
                **({"original_sources": originals} if originals else {}),
                "documents": [
                    {
                        "document_id": document.document_id,
                        "sha256": document.sha256,
                        "processing_status": document.processing_status,
                        "engine_type": document.engine_type,
                        "engine_id": document.engine_id,
                    }
                    for document in source_documents
                ],
            }
        )
    for item in pending_inbox:
        issues.append(
            {
                "code": "inbox_receipt_pending",
                "resource_id": item.item_id,
                "subject": item.filename,
                "message": (
                    item.last_error
                    if item.status == "failed" and item.last_error
                    else "收件箱文件尚未确认归档。"
                ),
                "filename": item.filename,
                "inbox_status": item.status,
                "action": {
                    "kind": "inline",
                    "path": "#monthly-close-inbox",
                    "label": "确认文件类型并归档",
                    "query": {},
                },
            }
        )
    return {
        "blocking_count": len(issues),
        "summary": {
            "sources": sources,
            "inbox_pending_count": len(pending_inbox),
        },
        "issues": issues,
    }


async def build_step_evidences(
    db: AsyncSession, cycle: MonthlyCloseCycle,
    *, financial_case: dict[str, Any] | None = None,
) -> list[StepEvidence]:
    from app.services.monthly_close.financial_case_bridge import (
        build_financial_case_snapshot, merge_financial_case_evidence,
    )
    if financial_case is None:
        financial_case = await build_financial_case_snapshot(db, cycle)
    source_collection = await _source_collection_snapshot(db, cycle, financial_case=financial_case)
    order_integrity = await order_integrity_snapshot(db, cycle)
    service_fees = await service_fees_snapshot(db, cycle)
    utilities = await utilities_snapshot(db, cycle)
    ota_statements = await ota_statements_snapshot(db, cycle)
    raw: dict[str, dict[str, Any]] = {
        "source_collection": source_collection,
        "order_integrity": order_integrity,
        "service_fees": service_fees,
        "utilities": utilities,
        "ota_statements": ota_statements,
        "exception_clearance": exception_clearance_snapshot(
            order_integrity, service_fees, utilities, ota_statements
        ),
        "preflight": await preflight_snapshot(db, cycle),
        "settlement_review": await settlement_review_snapshot(db, cycle),
        "owner_confirmation": await owner_confirmation_snapshot(db, cycle),
    }
    merge_financial_case_evidence(raw, financial_case)
    raw["exception_clearance"] = exception_clearance_snapshot(
        raw["order_integrity"], raw["service_fees"], raw["utilities"], raw["ota_statements"]
    )
    return build_evidence_chain(cycle.billing_month, raw)


async def build_role_workflow_evidence(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    *,
    actor_role: str,
) -> list[dict[str, Any]]:
    """Load full workflow evidence only for explicitly financial roles.

    The projection service calls this after resolving the actor role.  Keeping
    the role gate before ``build_step_evidences`` is intentional: low-privilege
    projections never query owner settlements, OTA amounts, unrelated orders,
    or other workflow evidence and therefore do not depend on JSON redaction
    after broad ORM loads.
    """
    if actor_role not in {"admin", "finance"}:
        return []
    return [
        {
            "step_key": evidence.step_key,
            "evidence_hash": evidence.evidence_hash,
            **evidence.snapshot,
        }
        for evidence in await build_step_evidences(db, cycle)
    ]
