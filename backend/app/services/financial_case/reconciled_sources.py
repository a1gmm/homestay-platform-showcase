"""Revalidate existing source reviews without posting, approving or inferring cash.

The original file, reviewed batch and current ledger must agree. An old green
badge alone is never evidence. Returned decisions are read-only report inputs;
the saved original and the administrator's interpretations remain unchanged.
"""
from __future__ import annotations

import asyncio
from decimal import Decimal
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.orm import undefer

from app.models.monthly_close import MonthlyCloseDocument
from app.models.recon import ReconBatch, ReconDiff, ReconDiffClass, ReconDiffStatus


async def _ota_review(db, cycle, source, document):
    from app.services.billing_recon.analysis import PlatformScope
    from app.services.billing_recon.engine import channels_for_scope, fetch_candidates, match_orders, classify, source_namespace
    from app.services.billing_recon.parser import (
        BillMapping, load_workbook_rows, extract_bill_rows, aggregate_orders, validate_bill,
    )
    if document.engine_type != 'billing_recon' or not document.engine_id:
        return None
    batch = await db.get(ReconBatch, document.engine_id, populate_existing=True)
    mapping = (batch.mapping or {}) if batch else {}
    if (not batch or batch.bill_month != cycle.billing_month or batch.status != 'parsed'
            or mapping.get('archived_at') or not mapping.get('reviewed_at')
            or not mapping.get('reviewed_by') or mapping.get('upload_fingerprint') != source.sha256):
        return None
    try:
        coordinates = BillMapping.model_validate(mapping)
        scope = PlatformScope(mapping['platform_scope'])
        if batch.platform != source_namespace(scope, mapping.get('layout_signature', '')):
            return None
        sheets, datemode = await asyncio.to_thread(load_workbook_rows, document.content, document.filename)
        rows = extract_bill_rows(sheets[coordinates.sheet], coordinates, datemode)
        errors, _ = validate_bill(rows, coordinates)
        bills = aggregate_orders(rows)
    except (ValueError, KeyError, TypeError):
        return None
    facts = source.parsed.get('facts', [])
    if errors or not bills or len(rows) != batch.row_count or sum((r.amount for r in rows), Decimal(0)) != batch.summary_total:
        return None
    # A re-parsed/edited source cannot borrow the review of a different set of rows.
    if (len(facts) != len(bills) or {f.get('platform_order_id') for f in facts} != set(bills)
            or any(f.get('kind') != 'platform_order' or f.get('amount') != format(bills[f['platform_order_id']].net, '.2f')
                   or f.get('date') != str(bills[f['platform_order_id']].checkout) for f in facts)):
        return None
    dates = [b.checkout for b in bills.values() if b.checkout]
    orders = await fetch_candidates(db, cycle.billing_month, min(dates) if dates else None,
                                    max(dates) if dates else None, channels_for_scope(scope))
    matches = match_orders(bills, orders)
    current = classify(bills, matches, orders, cycle.billing_month)
    diffs = list(await db.scalars(select(ReconDiff).where(ReconDiff.batch_id == batch.batch_id)))
    if any(d.status in {ReconDiffStatus.pending, ReconDiffStatus.appeal_pending} for d in diffs):
        return None
    # Only a still-identical, explicitly reviewed identity exception can survive
    # rechecking. Amount/compensation/appeal changes always return to review.
    for issue in current:
        if issue.diff_class != ReconDiffClass.broken_link:
            return None
        resolved = [d for d in diffs if d.diff_class == issue.diff_class
                    and d.platform_order_id == issue.platform_order_id and d.order_id == issue.order_id
                    and d.bill_amount == issue.bill_amount and d.system_amount == issue.system_amount
                    and d.resolved_by and d.resolved_at
                    and (d.status == ReconDiffStatus.acknowledged or
                         d.status == ReconDiffStatus.dismissed and (d.detail or {}).get('dismissal_reason'))]
        if len(resolved) != 1:
            return None
    return dict(kind='ota', source_id=source.source_id, document_id=document.document_id,
                batch_id=batch.batch_id, source_sha256=source.sha256,
                reviewed_by=mapping['reviewed_by'], reviewed_at=mapping['reviewed_at'],
                state='matched', fact_keys=[f['key'] for f in facts],
                matches=[dict(source_id=source.source_id, fact_key=f['key'],
                              order_id=matches[f['platform_order_id']].order.order_id
                              if matches[f['platform_order_id']].order else None,
                              batch_id=batch.batch_id, amount=f['amount']) for f in facts])


async def _cleaning_review(db, cycle, source, document):
    from app.services.monthly_close.cleaning_work_log import parse_work_document, compare_cleaning_work_log
    from app.services.monthly_close.work_record_fees import plan_work_fees
    entries = parse_work_document(document, cycle.billing_month)
    if not entries:
        return None
    comparison = await compare_cleaning_work_log(db, entries, cycle.billing_month,
                                                document.document_id, source_scope=True)
    fees = await plan_work_fees(db, document, cycle.billing_month)
    pending_fees = len(fees['actions']) + len(fees['unresolved']) + len(fees['blocked_actions'])
    return dict(kind='cleaning', source_id=source.source_id, document_id=document.document_id,
                source_sha256=source.sha256, state='matched' if not comparison.differences and not pending_fees else 'needs_review',
                record_count=comparison.record_count, matched_count=comparison.matched_count,
                pending_records=len(comparison.differences), pending_fees=pending_fees,
                comparison=comparison.model_dump(), fees=fees)


async def review_linked_sources(db, cycle, sources):
    ids = [s.document_id for s in sources if s.kind in {'ota', 'cleaning'} and s.document_id]
    documents = {d.document_id:d for d in await db.scalars(select(MonthlyCloseDocument)
        .options(undefer(MonthlyCloseDocument.content)).where(MonthlyCloseDocument.document_id.in_(ids)))}
    result = {}
    for source in sources:
        doc = documents.get(source.document_id)
        if (not doc or not doc.is_active or doc.cycle_id != cycle.cycle_id or doc.sha256 != source.sha256
                or sha256(doc.content).hexdigest() != source.sha256):
            continue
        review = await (_ota_review(db, cycle, source, doc) if source.kind == 'ota'
                        else _cleaning_review(db, cycle, source, doc))
        if review:
            result[source.source_id] = review
    from .source_acknowledgement import enrich_reviews
    await enrich_reviews(db, cycle, sources, result)
    return result


def report_decisions(sources, reviews):
    decisions = {key:dict(value) for s in sources for key,value in (s.decisions or {}).items()}
    for source in sources:
        review = reviews.get(source.source_id, {})
        if source.kind != 'ota' or review.get('state') != 'matched':
            continue
        for fact in source.parsed.get('facts', []):
            if decisions.get(fact['key'], {}).get('status') == 'confirmed':
                continue
            decisions[fact['key']] = dict(status='confirmed', category=fact['category'],
                business_month=fact['business_month'], include=True,
                note='原件与已复核平台批次及当前订单金额一致；银行到账仍单独核对',
                reviewed_batch_id=review['batch_id'])
    return decisions


def merge_live_reviews(sources, issues, matches, reviews):
    result = []
    for issue in issues:
        review = reviews.get(issue.get('source_id'), {})
        if review.get('kind') == 'ota' and review.get('state') == 'matched' and issue['code'] == 'platform_order_unmatched':
            continue
        if review.get('kind') == 'cleaning' and issue['code'] == 'cleaning_evidence_only':
            continue
        if review.get('kind') == 'linked_expenses' and issue['code'] == 'supplement_needs_details':
            continue
        # Domain caveats explain scope; they are never proof of an open money discrepancy.
        if issue['code'] == 'ctrip_cash_unconfirmed':
            continue
        result.append(issue)
    for source_id, review in reviews.items():
        if review['kind'] == 'cleaning':
            for key, code, label in [('pending_records','cleaning_records_pending','打扫记录'),
                                     ('pending_fees','cleaning_fees_pending','续住保洁费用')]:
                if review[key]:
                    result.append(dict(code=code, source_id=source_id, fact_key=None,
                        message=f"当前仍有 {review[key]} 项{label}待核实，请查看本表的具体差异。"))
    covered = {source_id for source_id,r in reviews.items() if r['kind']=='ota' and r['state']=='matched'}
    matches = [m for m in matches if m.get('source_id') not in covered]
    matches += [m for r in reviews.values() if r['kind']=='ota' for m in r['matches']]
    return result, matches


def report_sources(sources, reviews):
    """Only replace obsolete processing prompts after explicit bound confirmation."""
    from .service import source_payload
    result = []
    for source in sources:
        payload = source_payload(source)
        if reviews.get(source.source_id, {}).get('kind') == 'linked_expenses':
            payload = {**payload, 'parsed': {**payload['parsed'], 'issues': [
                i for i in payload['parsed'].get('issues', []) if i['code'] != 'supplement_needs_details']}}
        if reviews.get(source.source_id, {}).get('receipt_confirmation'):
            payload = {**payload, 'parsed': {**payload['parsed'], 'issues': [
                i for i in payload['parsed'].get('issues', []) if i['code'] != 'ctrip_cash_unconfirmed']}}
        result.append(payload)
    return result


def add_receipt_disclosures(report, reviews):
    for review in reviews.values():
        if review.get('receipt_confirmation'):
            note = f"平台净额 {review['receipt_confirmation']['amount']} 元已由管理员确认全部到账；未提供实际到账日期、账户或流水，不计作银行现金流凭据。"
            report['notes'].append(note)
            report['chat_details'].append(dict(label='平台到账人工确认', value=note))
            report.setdefault('receipt_confirmations', []).append(dict(
                source_id=review['source_id'], amount=review['receipt_confirmation']['amount'], note=note))
