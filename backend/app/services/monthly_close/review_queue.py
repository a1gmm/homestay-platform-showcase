"""Current owner-bill questions. Reading/answering never resolves a ledger issue.

The existing source matcher, preflight and receipt ledger remain authoritative.
No durable UI 'done' bit can suppress a new or changed financial discrepancy.
"""
from __future__ import annotations

from decimal import Decimal
from hashlib import sha256
import json
import logging

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


# Steps the administrator performs with one button; they are not questions.
ACTIONS = {
    "ota_statement_unreviewed": ("确认平台账单", "document",
                                 "打开这份平台账单，核对格式后点「确认」。和系统订单对上的会自动核销，只留下真正对不上的。"),
    "cleaning_records_pending": ("用保洁表补齐打扫记录", "document",
                                 "系统里的打扫记录和保洁表对不上。打开保洁表，先「预览补齐」看要补哪些，再「确认补齐」。"),
    "cleaning_fees_pending": ("核对续住保洁费用", "document", "打开保洁表，查看续住保洁费用的差异并确认处理方案。"),
    "document_review": ("打开这份资料", "document", None),
    "source_facts_missing": ("打开这份资料", "document", None),
    "expense_not_posted": ("生成费用记账方案", "command",
                           "这些费用已确认谁承担，但还没入账。生成方案看一遍，再点「确认执行」。"),
    "check_unavailable": ("重新检查", "refresh", None),
    "execution_pending": ("查看执行状态", "workflow", None),
    "bill_drift": ("复核并重新生成账单", "workflow", None),
    "disputed_bill": ("处理业主异议", "workflow", None),
    "offline_more": ("打开完整收款清单", "workflow", None),
}
ACTION_ORDER = list(ACTIONS)


def _group_id(*parts):
    return "RG-" + digest(list(parts))[:20]


def _group_cards(cards, offline, *, links, payer_default, receipt_eligible, source_fact_counts, sources):
    """Fold per-row cards into steps (one button) and questions (one decision).

    Cards stay the unit of answering and evidence; a group only says which cards
    belong to the same decision and, where safe, which existing executor command
    answers all of them at once. The command still goes through preview+confirm.
    """
    from app.services.monthly_close.review_linking import receipt_batch_command, utility_batch_command

    actions, questions, used = [], [], set()
    by_order = {c["order_id"]: c for c in cards if c["code"] == "offline_receipt_unconfirmed"}

    def action(code, members, **extra):
        label, target, message = ACTIONS[code]
        first = members[0]
        actions.append({"group_id": _group_id("action", code, *[m["question_id"] for m in members]),
                        "kind": "action", "code": code, "title": label if code != "document_review" and code != "source_facts_missing"
                        else f"{label}：{first.get('source_name') or first['subject']}",
                        "message": message or first["message"], "target": target,
                        "document_id": first.get("document_id"), "focus": first.get("focus"),
                        "subject": first.get("source_name") or first["subject"],
                        "member_ids": [m["question_id"] for m in members], "batch": None, **extra})
        used.update(m["question_id"] for m in members)

    posting = [c for c in cards if c["code"] == "expense_not_posted"]
    if posting:
        action("expense_not_posted", posting, command="生成记账方案", count=len(posting))
    for card in cards:
        if card["code"] in ACTIONS and card["question_id"] not in used:
            action(card["code"], [card])

    # A deleted platform order replaced by offline orders: one plain question.
    for link in links.get("replaced", []):
        members = [by_order[o["order_id"]] for o in link["offline"] if o["order_id"] in by_order]
        if not members:
            continue
        total = sum(Decimal(o["amount"] or 0) for o in link["offline"])
        rooms = "、".join(f"{o['room']}（¥{o['amount']}）" for o in link["offline"])
        questions.append({
            "group_id": _group_id("linked", link["platform_order_id"]), "kind": "question", "code": "platform_replaced_by_offline",
            "title": f"{link['guest_name']} 这单平台已结算 ¥{link['bill_amount']}，系统里是不是改成了线下单？",
            "message": (f"平台订单 {link['platform_order_id']}（{link['check_in']} 至 {link['check_out']}）在系统里的原单 "
                        f"{link['original_order_id']} 已删除；同一客人同期有线下单 {rooms}，合计应收 ¥{total:.2f}，都没有收款记录。"
                        "如果是同一笔，钱已经由平台付了，线下单不应再算一遍房费收入。请说明实际情况。"),
            "member_ids": [m["question_id"] for m in members], "answer_via": members[0]["question_id"], "batch": None,
            "flags": [],
        })
        used.update(m["question_id"] for m in members)

    # Utility payer rows: one decision per file, optionally pre-filled from last month.
    payer_by_source = {}
    for card in cards:
        if card["code"] == "payer_unconfirmed" and card["question_id"] not in used and card.get("fact_key"):
            payer_by_source.setdefault(card.get("source_id"), []).append(card)
    for source_id, members in payer_by_source.items():
        name = members[0].get("source_name") or "这份资料"
        batch = None
        note = "原表「已收」只说明供应方收到了钱，还要确认这笔费用由谁承担、谁先付的。"
        if payer_default:
            note += f" {payer_default['month']} 的 {payer_default['count']} 笔都是「{payer_default['phrase']}」。"
            # Whole-file wording only when every row of this file is still open.
            if source_id and len(members) == source_fact_counts.get(source_id):
                batch = {"label": f"{len(members)} 笔全部按「{payer_default['phrase']}」确认（同 {payer_default['month']}）",
                         "command": utility_batch_command(name, len(members), payer_default["phrase"]),
                         "attachment_ids": [source_id], "member_ids": [m["question_id"] for m in members]}
        questions.append({"group_id": _group_id("payer", source_id), "kind": "question", "code": "payer_unconfirmed",
                          "title": f"《{name}》这 {len(members)} 笔费用由谁承担？", "message": note,
                          "member_ids": [m["question_id"] for m in members], "batch": batch, "flags": []})
        used.update(m["question_id"] for m in members)

    # Offline receipts: one decision for the ordinary ones; flagged ones stay single.
    placeholder = set(links.get("placeholder", []))
    same_guest = links.get("same_guest", {})
    receipts = [c for c in cards if c["code"] == "offline_receipt_unconfirmed" and c["question_id"] not in used]
    if receipts:
        flags = {}
        for card in receipts:
            oid = card["order_id"]
            if oid in placeholder:
                flags.setdefault(card["question_id"], []).append("客人名只是占位，请先确认实际是谁")
            if same_guest.get(oid):
                flags.setdefault(card["question_id"], []).append(f"同一客人同日还订了 {'、'.join(same_guest[oid])}，确认是否同一人订多间")
        batchable = [c for c in receipts if c["order_id"] in receipt_eligible and c["order_id"] not in placeholder]
        batch = None
        if batchable:
            total = sum(Decimal(c["amount"] or 0) for c in batchable)
            batch = {"label": f"这 {len(batchable)} 笔都按应收全部收到（合计 ¥{total:.2f}）",
                     "command": receipt_batch_command(c["order_id"] for c in batchable), "attachment_ids": [],
                     "member_ids": [c["question_id"] for c in batchable]}
        questions.append({"group_id": _group_id("receipts"), "kind": "question",
                          "code": "offline_receipt_unconfirmed",
                          "title": f"这 {len(receipts)} 笔线下订单的房费收到了吗？",
                          "message": "系统里没有这些订单的收款记录。按应收全部收到的可以一起确认；金额不同或有标记的请单独填写。",
                          "member_ids": [c["question_id"] for c in receipts], "batch": batch,
                          "flags": [{"question_id": k, "messages": v} for k, v in flags.items()]})
        used.update(c["question_id"] for c in receipts)

    # Everything else: same code from the same file is one question.
    rest = {}
    for card in cards:
        if card["question_id"] not in used:
            rest.setdefault((card["code"], card.get("source_id")), []).append(card)
    for (code, source_id), members in rest.items():
        first = members[0]
        title = first["title"] if len(members) == 1 else f"{first['title']}（{len(members)} 项）"
        questions.append({"group_id": _group_id("rest", code, source_id), "kind": "question", "code": code,
                          "title": title, "message": first["message"],
                          "member_ids": [m["question_id"] for m in members], "batch": None, "flags": []})
    actions.sort(key=lambda g: ACTION_ORDER.index(g["code"]))
    return actions + questions


def compose_queue(projection, preflight, offline, *, fact_index=None, room_names=None, links=None,
                  payer_default=None, receipt_eligible=None, source_fact_counts=None):
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
    guest_names = (links or {}).get("guest_names", {})
    for row in offline.get("rows", []):
        if row.get("needs_review"):
            guest = guest_names.get(row["order_id"])
            add({"code": "offline_receipt_unconfirmed", "resource_id": row["order_id"],
                 "order_id": row["order_id"], "subject": f"{room_names.get(row.get('room'), row.get('room')) or '房间待核实'}{f' · {guest}' if guest else ''} · {row['checkout']}退房",
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
    groups = _group_cards(questions, offline, links=links or {}, payer_default=payer_default,
                          receipt_eligible=set(receipt_eligible or ()), source_fact_counts=source_fact_counts or {},
                          sources=sources)
    return {
        "billing_month": month, "cycle_id": projection["cycle_id"],
        "questions": questions, "groups": groups,
        # Zero only when nothing at all is open; shown as "N 组待回答 · M 步待操作".
        "pending_count": len(groups),
        "question_count": sum(g["kind"] == "question" for g in groups),
        "action_count": sum(g["kind"] == "action" for g in groups),
        "item_count": len(questions),
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
    loaded = await load_sources(db, cycle.cycle_id)
    facts = {(s.source_id, f["key"]): f for s in loaded for f in s.parsed.get("facts", [])}
    fact_counts = {s.source_id: len(s.parsed.get("facts", [])) for s in loaded}
    room_names = dict((await db.execute(select(Room.room_id, Room.room_name))).all())
    offline = await offline_review(db, cycle.billing_month)
    hints = {}
    try:
        # Read-only grouping hints. Losing them degrades to per-card answering.
        from app.services.monthly_close.review_linking import (
            load_review_links, previous_month_payer_default, receipt_batch_eligible)
        hints["links"] = await load_review_links(db, cycle, loaded, offline)
        hints["payer_default"] = await previous_month_payer_default(db, cycle)
        hints["receipt_eligible"] = await receipt_batch_eligible(
            db, cycle, [r["order_id"] for r in offline.get("rows", []) if r.get("needs_review")])
    except Exception:  # noqa: BLE001 - hints must never block the review queue
        logging.getLogger(__name__).exception("review queue hints unavailable")
        hints = {}
    return compose_queue(projection, await preflight_snapshot(db, cycle), offline,
                         fact_index=facts, room_names=room_names, source_fact_counts=fact_counts, **hints)


async def validate_review_answer(db, cycle, actor, context):
    queue = await build_review_queue(db, cycle, actor)
    card = next((q for q in queue["questions"] if q["question_id"] == context.question_id), None)
    if not card or card["evidence_hash"] != context.evidence_hash:
        raise HTTPException(409, detail={"code": "review_question_changed",
            "message": "这项问题已处理或依据已变化。请刷新待核清单后再回答；本次没有修改账目。"})
    return card
