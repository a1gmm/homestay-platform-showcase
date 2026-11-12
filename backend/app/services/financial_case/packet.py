"""Read-only monthly handoff: selected originals, work lanes and open questions.

This view never grants posting authority. Source totals are not ledger totals,
and identity matches are not amount matches. No original is sent to a model.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from decimal import Decimal
import re
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select

from app.models.order import BookingType, Channel, Order, OrderStatus
from app.models.payment import Payment
from app.models.refund import Refund, RefundReason


GUIDANCE_CODES = frozenset({
    "cleaning_evidence_only", "ctrip_cash_unconfirmed", "worksheet_dimensions_recovered",
})


def wants_packet(text: str) -> bool:
    if re.search(r"我的补充：(?:这组|这项)?(?:暂时无法确认|暂时不知道)", text):
        return True
    # A request to confirm/change something must retain its controlled handler.
    # A historical report mentioned as excluded evidence is not a report request.
    intent = re.sub(r"(?:不要|不把|别把|勿把)[^。；;\n]*报告[^。；;\n]*", "", text)
    if re.search(r"确认执行|生成.*方案|补齐|补录|删除|改成|承担|垫付|利润|导出", text) or '报告' in intent:
        return False
    return bool(re.search(r"(?:整理|核对|查看).{0,10}(?:这批|这些|本月|月结|对账)资料|"
                          r"请查看我刚上传的月结资料|按.{0,8}(?:资料|发的).{0,6}核对|"
                          r"只有携程和线下|查看本月线下订单与收款", text))


def supplementary_note(text: str) -> str | None:
    """Capture the clause, never guess a split, date, room or accounting amount."""
    if text.startswith("关于以下问题："):
        return None
    clauses = re.split(r"[\n。；;]", text)
    notes = [clause.strip() for clause in clauses
             if re.search(r"再加|补充|另有|还有|另外", clause)
             and re.search(r"费用|电费|水费|打扫|保洁|维修", clause)
             and re.search(r"\d", clause)
             and not re.search(r"已.{0,12}(?:入账|记账|录入)|EXP-[A-Za-z0-9]+", clause)]
    return "；".join(notes)[:500] or None


async def receive_note(db, cycle, actor, text):
    """Archive a stated supplemental amount as an unconfirmed evidence candidate.

    Reuse the image-candidate review contract: category, room, period, amount,
    payer and actual payer must be confirmed before the normal posting preview.
    A mixed-purpose sentence can never choose its own expense category.
    """
    from app.models.financial_case import FinancialCaseSource
    from app.services.monthly_close.workflow import require_cycle_writable
    from app.services.audit import log_action_tx
    from .service import actor_id, require_admin, load_sources
    from .image_facts import _candidate, _category, _decimal
    await require_admin(db, actor)
    await require_cycle_writable(db, cycle)
    digest = sha256(("supplement-v1:" + text).encode()).hexdigest()
    existing = await db.scalar(select(FinancialCaseSource).where(
        FinancialCaseSource.cycle_id == cycle.cycle_id, FinancialCaseSource.sha256 == digest))
    if existing:
        return existing
    current_sources = await load_sources(db, cycle.cycle_id)
    if len(current_sources) >= 100:
        raise ValueError('一个对账工作区最多保存 100 份来源，请按月份分别整理')
    amount_match = re.search(r"(?<![\d.年月/,-])([+-]?\d[\d,]*(?:\.\d{1,2})?)\s*(?:元|块)?\s*$", text)
    amount = _decimal(amount_match[1]) if amount_match else None
    labelled_amounts = re.findall(r"(?:费用|电费|水费|打扫|保洁|维修)[^\d]{0,4}\d[\d,]*(?:\.\d{1,2})?", text)
    if len(re.findall(r"\d[\d,]*(?:\.\d{1,2})?\s*(?:元|块)", text)) > 1 or len(labelled_amounts) > 1 or re.search(r"退款|退费|冲正", text):
        amount = None  # Multiple amounts/refunds require a split, never the last number alone.
    category = _category(text)
    if re.search(r"打扫|卫生", text) and category not in ('unknown', 'cleaning_supplier'):
        category = 'unknown'
    candidate = _candidate(digest, 1, amount, category, text,
                           basis="supplemental_message") if amount is not None else None
    if candidate:
        candidate.update(sheet="补充说明", description="补充费用候选，金额与用途待核实")
        candidate['evidence_refs'] = [dict(sheet="补充说明", row1based=1, role="original_message", text=text)]
    parsed = dict(kind="note", facts=[candidate] if candidate else [], summaries=[], templates=[],
                  issues=[] if candidate else [dict(code="supplement_needs_details", message="补充说明已保存，但金额无法可靠识别。请补充逐笔费用与凭据。")])
    source = FinancialCaseSource(source_id="FCS-" + uuid4().hex[:20].upper(),
        cycle_id=cycle.cycle_id, filename=f"补充费用说明 {1 + sum(s.kind == 'note' for s in current_sources)}.txt",
        sha256=digest, kind="note", content=text.encode(), parsed=parsed,
        decisions={}, version=1, created_by=actor_id(actor))
    db.add(source)
    await db.flush()
    await log_action_tx(db, actor_id(actor), 'financial_case.source_received', 'financial_case_source', source.source_id,
                        after_data={'sha256': digest, 'kind': 'note', 'facts': len(parsed['facts'])})
    return source


def source_card(source, month, all_sources, matched):
    facts = source.parsed.get("facts", [])
    current = [f for f in facts if f.get("business_month") == month]
    other = len(facts) - len(current)
    kind = source.kind
    label = {"ota": "平台账单", "cleaning": "保洁打扫", "utility": "水电等费用", "bank": "收付款流水", "note": "补充费用说明"}.get(kind, "其他资料")
    summary = f"读到 {len(facts)} 条明细"
    note = "已保存原件，请核实内容。"
    if kind == "cleaning":
        normal = sum(f.get("fields", {}).get("service_type") == "cleaning" for f in current)
        instay = sum(f.get("fields", {}).get("service_type") == "instay_cleaning" for f in current)
        summary = f"本月 {len(current)} 次：正常打扫 {normal} 次，续住打扫 {instay} 次"
        note = "接着对照系统打扫记录；次数不直接换算费用。"
    elif kind == "ota":
        orders = [f for f in current if f.get("kind") == "platform_order"]
        if orders:
            amount = sum((Decimal(f["amount"]) for f in orders), Decimal(0))
            summary = f"本月 {len(orders)} 个订单，平台净结算 {amount:,.2f} 元"
        note = "调整行已按订单合并，折扣明细不重复相加；继续核对系统订单与金额，银行到账另核。"
    elif kind == "utility":
        amounts = defaultdict(Decimal)
        for fact in current:
            if fact.get("kind") == "source_expense" and fact.get("amount") is not None:
                amounts[fact.get("category")] += Decimal(fact["amount"])
        summary = f"本月 {len(current)} 笔，原表金额 {sum(amounts.values(), Decimal(0)):,.2f} 元"
        note = "供应方的“已收”是水电等费用，不是线下房费收入。先核对已有费用，再补充承担方和付款方。"
    elif kind == "note":
        corresponding = [m for m in matched['matches'] if m.get('source_id') == source.source_id and m.get('expense_ids')]
        summary = f"已对应 {len(corresponding)} 笔系统支出" if corresponding else "原话已保存，费用尚未入账"
        note = "；".join(f.get('original_text', '') for f in facts) or "请补充逐笔金额、日期、用途和付款凭据。"
    if other and kind != 'note':
        note += f" 另有 {other} 条属于其他月份或月份待确认，本月数字未计入。"
    versions = sum(s.source_id != source.source_id and s.filename == source.filename
                   and s.sha256 != source.sha256 for s in all_sources)
    return dict(source_id=source.source_id, document_id=source.document_id,
                filename=source.filename, kind=kind, label=label, summary=summary,
                note=note, other_version_count=versions)


async def offline_review(db, month):
    year, number = map(int, month.split("-"))
    start = date(year, number, 1)
    end = date(year + (number == 12), number % 12 + 1, 1)
    channels = [Channel.offline, Channel.walk_in, Channel.self_acquired, Channel.private, Channel.direct]
    orders = list((await db.scalars(select(Order).where(
        Order.is_deleted.is_(False), Order.channel.in_(channels),
        Order.order_status.notin_([OrderStatus.cancelled, OrderStatus.rescheduled]),
        Order.booking_type != BookingType.owner_self,
        Order.check_out_date >= start, Order.check_out_date < end,
    ).order_by(Order.check_out_date, Order.order_id))).all())
    ids = [o.order_id for o in orders]
    payments = list((await db.scalars(select(Payment).where(
        Payment.order_id.in_(ids), Payment.is_deleted.is_(False), Payment.is_deposit.is_(False),
    ))).all()) if ids else []
    refunds = list((await db.scalars(select(Refund).where(
        Refund.order_id.in_(ids), Refund.is_deleted.is_(False), Refund.reason != RefundReason.deposit_return,
    ))).all()) if ids else []
    from app.models.order_room import OrderRoom
    from app.services.reconciliation_policy import confirmed_receipt_amount
    legs=list(await db.scalars(select(OrderRoom).where(OrderRoom.order_id.in_(ids)))) if ids else []
    paid, returned = defaultdict(Decimal), defaultdict(Decimal)
    for payment in payments:
        paid[payment.order_id] += payment.amount
    for refund in refunds:
        returned[refund.order_id] += refund.amount
    rows = []
    for order in orders:
        net = paid[order.order_id] - returned[order.order_id]
        comparable = order.actual_price is not None and order.order_status not in (OrderStatus.cancelled, OrderStatus.rescheduled)
        difference = order.actual_price - net if comparable else None
        confirmed = confirmed_receipt_amount(order,[r for r in legs if r.order_id==order.order_id],payments,refunds,month) if comparable else None
        rows.append(dict(order_id=order.order_id, room=order.room_id or "待核实房间",
                         checkout=str(order.check_out_date), amount=f"{order.actual_price:.2f}" if order.actual_price is not None else None,
                         received=f"{paid[order.order_id]:.2f}", refunded=f"{returned[order.order_id]:.2f}",
                         difference=f"{difference:.2f}" if difference is not None else None,
                         confirmed_received=f'{confirmed:.2f}' if confirmed is not None else None,
                         needs_review=(confirmed != order.actual_price) if confirmed is not None else (difference is None or difference != 0)))
    pending = [r for r in rows if r["needs_review"]]
    confirmed_rows = [r for r in rows if r["confirmed_received"] is not None]
    return dict(count=len(rows), pending_count=len(pending),
                confirmed_count=len(confirmed_rows),
                confirmed_amount=f"{sum((Decimal(r['confirmed_received']) for r in confirmed_rows), Decimal(0)):.2f}",
                rows=(pending + [r for r in rows if not r["needs_review"]])[:200],
                truncated=len(rows) > 200,
                note="按本月离店的线下、自来客及对应历史渠道订单查询；房费收款减房费退款，不含押金及作废记录。收退款跨月也按该订单累计，不是本月银行流水；金额一致仍需核对收款凭据。")


async def build_packet(db, cycle, sources, all_sources, matched, notes):
    groups = {}
    for issue in matched["issues"]:
        if issue["code"] in GUIDANCE_CODES:
            continue
        key = (issue.get("source_id"), issue["code"])
        group = groups.setdefault(key, dict(source_id=issue.get("source_id"), code=issue["code"], count=0, examples=[]))
        group["count"] += 1
        if len(group["examples"]) < 3:
            group["examples"].append(issue["message"])
    titles = {
        "payer_unconfirmed": "这些费用由谁承担、谁实际付款？",
        "room_unmapped": "原表房号需要对应系统房间",
        "platform_order_unmatched": "平台订单未唯一对应系统订单",
        "duplicate_source_expense": "同一笔费用出现在多处资料中",
    }
    for group in groups.values():
        group["title"] = titles.get(group["code"], group["examples"][0])[:160]
    return dict(
        sources=[source_card(s, cycle.billing_month, all_sources, matched) for s in sources],
        questions=list(groups.values()), notes=list(dict.fromkeys(notes))[-10:],
        excluded_source_count=len(all_sources) - len(sources),
        offline=await offline_review(db, cycle.billing_month),
    )
