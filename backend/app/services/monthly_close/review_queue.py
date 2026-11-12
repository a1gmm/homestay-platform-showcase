"""Current owner-bill questions. Reading/answering never resolves a ledger issue.

The existing source matcher, preflight and receipt ledger remain authoritative.
No durable UI 'done' bit can suppress a new or changed financial discrepancy.
"""
from __future__ import annotations

from hashlib import sha256
import json

from fastapi import HTTPException

from app.services.financial_case.service import require_admin


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()


COMPANY_ONLY = {"bank_evidence_missing", "requested_month_evidence_missing", "no_evidence",
                "owner_distribution_basis_unknown", "owner_distribution_basis_mixed"}
GUIDANCE_ONLY = {"cleaning_evidence_only", "worksheet_dimensions_recovered"}
TITLES = {
    "payer_unconfirmed": "这笔费用由谁承担？",
    "owner_expense_payment_unknown": "这笔业主费用是谁实际付的？",
    "image_payment_unconfirmed": "这笔费用是谁实际付的？",
    "room_unresolved": "这条记录对应哪间房？",
    "expense_not_posted": "已核实的费用还需要确认记账方案",
    "offline_receipt_unconfirmed": "这笔线下房费实际收到了多少？",
    "source_facts_missing": "这份资料还没有读出可核对的明细",
    "ota_statement_unreviewed": "这份平台账单还没完成平台对账",
    "bill_drift": "账单依据发生变化，需要重新复核",
    "disputed_bill": "业主账单有异议，需要处理",
}


def compose_queue(projection, preflight, offline, *, fact_index=None, room_names=None):
    """Pure presentation; keep distinct rows, collapse identical current issues."""
    month = projection["billing_month"]
    financial = projection.get("financial_case") or {}
    sources = {s["source_id"]: s for s in financial.get("sources", [])}
    questions = {}
    other = []
    fact_index = fact_index or {}
    room_names = room_names or {}

    def add(raw, group, focus="preflight"):
        code = raw["code"]
        if code in GUIDANCE_ONLY:
            return
        if code in COMPANY_ONLY:
            other.append({"code": code, "message": raw["message"]})
            return
        resource = raw.get("fact_key") or raw.get("resource_id") or raw.get("order_id") or code
        identity = [month, raw.get("source_id"), resource, code]
        key = "RQ-" + digest(identity)[:24]
        source = sources.get(raw.get("source_id"), {})
        fact = fact_index.get((raw.get("source_id"), raw.get("fact_key")), {})
        fact_context = " · ".join(str(v) for v in (fact.get("room_ref"), fact.get("date"), fact.get("description")) if v)
        card = {
            "question_id": key, "code": code, "group": group,
            "title": TITLES.get(code, "请核实这项差异"),
            "subject": fact_context or raw.get("subject") or source.get("filename") or "本月账务检查",
            "message": raw["message"], "amount": raw.get("amount"),
            "source_id": raw.get("source_id"), "source_name": source.get("filename"),
            "fact_key": raw.get("fact_key"),
            "source_row_label": f"{fact.get('sheet', '原表')} 第 {fact.get('row1based', fact.get('row'))} 行"
            if fact.get('row1based', fact.get('row')) else None,
            "document_id": raw.get("document_id") or source.get("document_id"), "order_id": raw.get("order_id"),
            "focus": focus, "suggestions": [],
        }
        # Suggestions describe facts only. They are not approvals or postings.
        if code in {"payer_unconfirmed", "owner_expense_payment_unknown", "image_payment_unconfirmed"}:
            card["suggestions"] = ["公司垫付，业主承担", "公司支付，公司承担", "业主自己支付并承担"]
        card["evidence_hash"] = digest({"issue": raw, "source_version": source.get("version"),
                                        "source_snapshot": financial.get("snapshot_hash") if source else None})
        questions[key] = card

    for issue in preflight.get("issues", []):
        add(issue, "账单检查")
    for issue in financial.get("pending_issues", []):
        add(issue, "原始资料", issue.get("step_key", "preflight"))
    for source in projection.get("sources", []):
        for doc in source.get("documents", []):
            if any(q["document_id"] == doc["document_id"] for q in questions.values()):
                continue
            message = None
            if doc.get("work_review_state") in {"records_pending", "fees_pending"}:
                message = "打扫记录或续住费用仍有差异，请打开这份保洁表核实并确认处理方案。"
            elif doc.get("analysis_state") in {"failed", "needs_mapping", "not_started", "queued", "running"}:
                message = {"failed": "这份资料识别失败，请查看原因并重试，已保存的原件会保留。",
                           "needs_mapping": "请核对这份表格的金额、日期和房间列。"}.get(doc["analysis_state"], "这份资料尚未完成识别核对，请查看处理进度。")
            if message:
                add({"code": "document_review", "resource_id": doc["document_id"], "document_id": doc["document_id"],
                     "subject": doc.get("filename"), "message": message, "state": doc}, "原始资料", source["source_type"])
    for row in offline.get("rows", []):
        if row.get("needs_review"):
            add({"code": "offline_receipt_unconfirmed", "resource_id": row["order_id"],
                 "order_id": row["order_id"], "subject": f"{room_names.get(row.get('room'), row.get('room')) or '房间待核实'} · {row['checkout']}退房",
                 "amount": row["amount"], "receipt": row,
                 "message": f"应收 {row['amount']} 元，系统收款记录 {row['received']} 元，退款 {row['refunded']} 元。请填写实际收到的金额；没有记录不代表没有收到。"}, "线下收款")
    shown_offline = sum(q["code"] == "offline_receipt_unconfirmed" for q in questions.values())
    if offline["pending_count"] > shown_offline:
        add({"code": "offline_more", "message": f"另有 {offline['pending_count'] - shown_offline} 笔线下收款待核实，请打开完整收款清单。"}, "线下收款")
    progress = projection.get("business_progress") or {}
    review_state = projection.get("final_review", {}).get("state")
    if review_state in {"approved", "executing", "verifying", "succeeded_unverified", "unknown", "remediation"}:
        add({"code": "execution_pending", "message": "上次处理仍在执行、复核或等待结果确认，请先查看执行状态，不要重复提交。", "state": review_state}, "处理进度", "final_review")
    for field, code, message in (("bill_drift_count", "bill_drift", "账单金额与当前订单或费用不一致，请复核并重新生成。"),
                                 ("disputed_bill_count", "disputed_bill", "请核实业主提出的异议，再确认账单。")):
        if progress.get(field):
            add({"code": code, "message": message, "count": progress[field]}, "业主账单", "final_review")
    if progress.get("billing_check") not in {"passed", "needs_review"}:
        add({"code": "check_unavailable", "message": "账单检查尚未取得结果，请重新检查后再查看最终账单。"}, "账单检查")
    questions = sorted(questions.values(), key=lambda q: (q["group"], q["question_id"]))
    return {
        "billing_month": month, "cycle_id": projection["cycle_id"],
        "questions": questions, "pending_count": len(questions),
        "company_notes": list({i["code"] + i["message"]: i for i in other}.values()),
        "snapshot_hash": digest(questions),
        "checked_at": projection["computed_at"],
        "scope_note": "这里集中列出影响业主账单的当前待核事项。回答后会重新核查；需要改账的事项仍须确认处理方案。公司经营账和实际打款分别处理。",
    }


async def build_review_queue(db, cycle, actor, *, projection=None):
    try:
        await require_admin(db, actor)
    except PermissionError as exc:
        raise HTTPException(403, detail="月结助手仅管理员可用") from exc
    from app.services.monthly_close.workflow import build_role_cycle_projection
    from app.services.monthly_close.adapters import preflight_snapshot
    from app.services.financial_case.packet import offline_review
    from app.services.financial_case.service import load_sources
    from app.models.room import Room
    from sqlalchemy import select
    if projection is None:
        projection = await build_role_cycle_projection(db, cycle, actor)
    facts = {(s.source_id, f["key"]): f for s in await load_sources(db, cycle.cycle_id) for f in s.parsed.get("facts", [])}
    room_names = dict((await db.execute(select(Room.room_id, Room.room_name))).all())
    return compose_queue(projection, await preflight_snapshot(db, cycle),
                         await offline_review(db, cycle.billing_month), fact_index=facts, room_names=room_names)


async def validate_review_answer(db, cycle, actor, context):
    queue = await build_review_queue(db, cycle, actor)
    card = next((q for q in queue["questions"] if q["question_id"] == context.question_id), None)
    if not card or card["evidence_hash"] != context.evidence_hash:
        raise HTTPException(409, detail={"code": "review_question_changed",
            "message": "这项问题已处理或依据已变化。请刷新待核清单后再回答；本次没有修改账目。"})
    return card
