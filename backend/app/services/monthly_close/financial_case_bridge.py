"""Read-only, live issue/evidence bridge for original-file financial cases.

The ledger matcher remains the authority for accounting progress. Archiving a
file or confirming its interpretation does not post it or confirm a close step.
This module deliberately has no dependency on monthly-close evidence/workflow.
"""
from __future__ import annotations

from hashlib import sha256
import json
from typing import Any

KIND_SOURCE_TYPES = {
    "cleaning": "cleaning_statement",
    "utility": "utility_expense",
    "ota": "ota_statement",
}
CATEGORY_SOURCE_TYPES = {
    "water": "utility_expense", "electricity": "utility_expense", "utility": "utility_expense",
    "cleaning_supplier": "cleaning_statement", "cleaning_supplier_cost": "cleaning_statement",
    "laundry_supplier": "linen_statement", "laundry_supplier_cost": "linen_statement",
    "property": "operating_expenses", "property_fee": "operating_expenses",
    "maintenance": "operating_expenses", "supplies": "operating_expenses",
    "rent": "operating_expenses", "operating_expense": "operating_expenses",
    "payroll": "operating_expenses", "social_insurance": "operating_expenses",
    "tax": "operating_expenses", "bank_fee": "operating_expenses",
}
SOURCE_STEPS = {
    "cleaning_statement": "service_fees", "linen_statement": "service_fees",
    "utility_expense": "utilities", "operating_expenses": "utilities",
    "ota_statement": "ota_statements",
}


def _source_types(source: Any) -> list[str]:
    types = set()
    if source.kind in KIND_SOURCE_TYPES:
        types.add(KIND_SOURCE_TYPES[source.kind])
    for fact in source.parsed.get("facts", []):
        decision = (source.decisions or {}).get(fact.get("key"), {})
        if fact.get("kind") == "image_expense_candidate" and decision.get("status") != "confirmed":
            continue
        if fact.get("kind") not in {"source_expense", "image_expense_candidate", "bank_transaction"}:
            continue
        category = decision.get("category", fact.get("category"))
        if category in CATEGORY_SOURCE_TYPES:
            types.add(CATEGORY_SOURCE_TYPES[category])
    return sorted(types)


def _issue_type(source: Any, fact_key: str | None) -> str | None:
    fact = next((f for f in source.parsed.get("facts", []) if f.get("key") == fact_key), {})
    decision = (source.decisions or {}).get(fact_key, {})
    category = decision.get("category", fact.get("category"))
    return CATEGORY_SOURCE_TYPES.get(category) or KIND_SOURCE_TYPES.get(source.kind)


def normalize_financial_case_issues(sources: list[Any], issues: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Canonical live issues shared by chat, checklist and monthly-close evidence.

    Generic immutable OCR caveats are replaced by candidate-level checks; these
    checks must be supplied by the live matcher/report, never by upload alone.
    """
    by_id = {source.source_id: source for source in sources}
    result: dict[tuple, dict] = {}
    for issue in issues:
        source = by_id.get(issue.get("source_id"))
        if source and issue["code"] in {
            "image_requires_verification", "image_candidates_unconfirmed",
            "image_expense_category_unknown", "image_discount_allocation_required",
        } and any(f.get("kind") == "image_expense_candidate" for f in source.parsed.get("facts", [])):
            continue
        code = {"expense_payer_unconfirmed": "payer_unconfirmed"}.get(issue["code"], issue["code"])
        identity = (issue.get("source_id"), issue.get("fact_key"), code,
                    str(issue.get("fact_key") or issue.get("message") or issue.get("source_id") or code))
        result.setdefault(identity, {**issue, "code": code})
    precise_payers = {i.get("source_id") for i in result.values()
                      if i["code"] == "payer_unconfirmed" and i.get("fact_key")}
    return [i for i in result.values() if not (
        i["code"] == "payer_unconfirmed" and not i.get("fact_key") and i.get("source_id") in precise_payers)]


async def reconcile_posted_other_month_facts(db: Any, cycle: Any, sources: list[Any], matched: dict) -> dict:
    """Clear a foreign-month prompt only after revalidating its bound ledger row.

    This only reads already posted facts; it never infers a new posting scope or
    adds another month's operations. Call after matching, not within match_sources.
    """
    from app.services.financial_case.service import match_sources
    from app.services.financial_case.cross_month import cycle_view

    facts = {(source.source_id, fact["key"]): (source, fact)
             for source in sources for fact in source.parsed.get("facts", [])}
    candidates = {(issue.get("source_id"), issue.get("fact_key"))
                  for issue in matched["issues"] if issue["code"] == "expense_period_unknown"}
    months: dict[str, set[tuple]] = {}
    for identity in candidates:
        source_fact = facts.get(identity)
        if source_fact is None:
            continue
        source, fact = source_fact
        month = (source.decisions or {}).get(fact["key"], {}).get("business_month", fact.get("business_month"))
        if not month or month == cycle.billing_month:
            continue
        if any(not row.is_deleted and row.notes == f"financial-case:{source.source_id}:{fact['key']}"
               and row.expense_date.strftime("%Y-%m") == month for row in matched["ledger"]):
            months.setdefault(month, set()).add(identity)
    verified: dict[tuple, dict] = {}
    for month, identities in sorted(months.items()):
        result = await match_sources(db, cycle_view(cycle, month), sources)
        for match in result["matches"]:
            identity = (match.get("source_id"), match.get("fact_key"))
            if identity in identities and match.get("expense_ids") and not any(
                (issue.get("source_id"), issue.get("fact_key")) == identity for issue in result["issues"]
            ):
                verified[identity] = match
    if not verified:
        return matched
    return {**matched,
            "issues": [issue for issue in matched["issues"] if not (
                issue["code"] == "expense_period_unknown"
                and (issue.get("source_id"), issue.get("fact_key")) in verified)],
            "matches": matched["matches"] + list(verified.values())}


async def build_financial_case_snapshot(db: Any, cycle: Any) -> dict[str, Any]:
    # Lazy import avoids the existing service -> workflow -> evidence import cycle.
    from app.services.financial_case.service import load_sources, match_sources, source_payload
    from app.services.financial_case.reporting import build_report

    sources = [source for source in await load_sources(db, cycle.cycle_id)
               if source.kind not in {"feedback", "checklist"}]
    if not sources:
        return {"sources": [], "pending_issues": [], "snapshot_hash": None}
    matched = await match_sources(db, cycle, sources)
    matched = await reconcile_posted_other_month_facts(db, cycle, sources, matched)
    report = build_report([source_payload(s) for s in sources],
                          {key: value for s in sources for key, value in (s.decisions or {}).items()},
                          [cycle.billing_month])
    by_id = {source.source_id: source for source in sources}
    pending: dict[tuple, dict] = {}

    def add_issue(raw: dict[str, Any]) -> None:
        source = by_id.get(raw.get("source_id"))
        code = {"expense_payer_unconfirmed": "payer_unconfirmed"}.get(raw["code"], raw["code"])
        fact_key = raw.get("fact_key")
        # Source-level report warnings are superseded by precise per-row matcher issues.
        if code == "payer_unconfirmed" and not fact_key and any(
            i.get("source_id") == raw.get("source_id") and i.get("code") == code
            for i in pending.values()
        ):
            return
        resource = str(fact_key or raw.get("source_id") or code)
        if not fact_key:
            # Preserve distinct source-level errors (for example different bank
            # balance rows) while deduplicating the same warning from two readers.
            resource += ":" + sha256(str(raw.get("message") or code).encode()).hexdigest()[:12]
        identity = (raw.get("source_id"), fact_key, code, resource)
        if identity in pending:
            return
        source_type = _issue_type(source, fact_key) if source else None
        issue_key = "financial_case:" + sha256(json.dumps(identity).encode()).hexdigest()[:24]
        pending[identity] = {
            **raw, "code": code, "issue_key": issue_key, "resource_id": resource,
            "source_id": source.source_id if source else None,
            "source_version": source.version if source else None,
            "fact_key": fact_key, "source_type": source_type,
            "step_key": SOURCE_STEPS.get(source_type, "source_collection"),
            "origin": "financial_case",
            "subject": source.filename if source else "原件财务核对",
            "action": {"kind": "inline", "path": "#monthly-close-originals",
                       "label": "在原件工作台核实并确认处理方案", "query": {}},
        }

    for issue in normalize_financial_case_issues(
        sources, matched["issues"] + [i for i in report["issues"] if i.get("blocking")]
    ):
        add_issue(issue)
    for operation in matched["operations"]:
        add_issue({"code": "expense_not_posted", "source_id": operation["source_id"],
                   "fact_key": operation["fact_key"], "amount": operation["amount"],
                   "message": "原件解释已确认，这笔费用尚未入账；请查看并确认记账方案。"})
    for source in sources:
        if not source.parsed.get("facts") and not any(i.get("source_id") == source.source_id for i in pending.values()):
            add_issue({"code": "source_facts_missing", "source_id": source.source_id,
                       "message": "原件已保存，但没有可核对的业务明细，请补充资料或核实文件结构。"})

    source_summaries = []
    for source in sources:
        facts = source.parsed.get("facts", [])
        decisions = source.decisions or {}
        source_pending = [i for i in pending.values() if i.get("source_id") == source.source_id]
        matches = [m for m in matched["matches"] if m.get("source_id") == source.source_id]
        expense_keys = {f["key"] for f in facts if f.get("kind") in {"source_expense", "image_expense_candidate"}}
        excluded = {f["key"] for f in facts if decisions.get(f["key"], {}).get("status") == "confirmed"
                    and decisions.get(f["key"], {}).get("include") is False}
        matched_expense_keys = {m["fact_key"] for m in matches if m.get("expense_ids")}
        # Confirmed bank expenses use the same live posting/match lineage.
        expense_keys |= matched_expense_keys | {op["fact_key"] for op in matched["operations"]
                                               if op["source_id"] == source.source_id}
        posted_keys = {m["fact_key"] for m in matches if m.get("expense_ids") and any(
            row.expense_id in m["expense_ids"] and row.notes == f"financial-case:{source.source_id}:{m['fact_key']}"
            for row in matched["ledger"])}
        reviewed_count = sum(decisions.get(f["key"], {}).get("status") == "confirmed" for f in facts)
        expense_complete = bool(expense_keys) and expense_keys <= matched_expense_keys | excluded and not source_pending
        state = ("reviewed" if all(i["code"] == "expense_not_posted" for i in source_pending) else "needs_review") if source_pending else (
            "posted" if expense_complete and posted_keys else "matched" if expense_complete and matched_expense_keys
            else "reviewed" if reviewed_count and reviewed_count == len(facts) else "parsed")
        source_summaries.append({
            "source_id": source.source_id, "filename": source.filename, "kind": source.kind,
            "version": source.version, "document_id": source.document_id,
            "sha256": source.sha256, "source_types": _source_types(source), "state": state,
            "fact_count": len(facts), "reviewed_fact_count": reviewed_count,
            "posted_fact_count": len(posted_keys), "matched_fact_count": len({m["fact_key"] for m in matches}),
            "pending_count": len(source_pending), "expense_complete": expense_complete,
            "has_expense_facts": bool(expense_keys),
        })
    issues = sorted(pending.values(), key=lambda i: i["issue_key"])
    material = {"sources": source_summaries, "pending_issues": issues,
                "ledger_snapshot_hash": matched["snapshot_hash"]}
    return {"sources": source_summaries, "pending_issues": issues,
            "snapshot_hash": sha256(json.dumps(material, sort_keys=True, default=str).encode()).hexdigest()}


def merge_financial_case_evidence(raw_by_step: dict[str, dict], snapshot: dict[str, Any]) -> None:
    """Replace only superseded file-processing warnings; retain financial controls."""
    if not snapshot["sources"]:
        return
    original_utility_documents = {s["document_id"] for s in snapshot["sources"]
                                  if s["document_id"] and s["has_expense_facts"] and s["kind"] == "utility"}
    completed_utility_documents = {s["document_id"] for s in snapshot["sources"]
                                   if s["document_id"] and s["expense_complete"] and s["kind"] == "utility"}
    utilities = raw_by_step["utilities"]
    utilities["issues"] = [i for i in utilities["issues"] if not (
        i.get("code") == "utility_document_unprocessed" and i.get("document_id") in original_utility_documents)]
    if "processed_utility_document_count" in utilities["summary"]:
        archived_documents = [document for source in raw_by_step["source_collection"]["summary"]["sources"]
                              for document in source["documents"]]
        utilities["summary"]["processed_utility_document_count"] += sum(
            document["document_id"] in completed_utility_documents and document["processing_status"] != "processed"
            for document in archived_documents
        )
    for issue in snapshot["pending_issues"]:
        raw_by_step[issue["step_key"]]["issues"].append(issue)
    for step in {"source_collection", "service_fees", "utilities", "ota_statements"}:
        raw = raw_by_step[step]
        raw["blocking_count"] = len(raw["issues"])
        relevant = [s for s in snapshot["sources"] if any(SOURCE_STEPS.get(t) == step for t in s["source_types"])]
        raw["summary"]["financial_case_sources"] = relevant
    # Version/decision/ledger changes invalidate the entire dependent evidence chain.
    raw_by_step["source_collection"]["summary"]["financial_case_snapshot_hash"] = snapshot["snapshot_hash"]
