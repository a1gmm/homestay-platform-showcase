"""Explicit source confirmations; never create cash, expenses or owner consent.

Use the existing actor-bound proposal and month lock contract. Confirmations
remain valid only while their exact source and linked accounting evidence agree.
"""
from datetime import datetime, timezone
from decimal import Decimal
import re
from uuid import uuid4

from sqlalchemy import select

from app.models.financial_case import FinancialCaseProposal
from app.models.monthly_close import MonthlyCloseDocument
from app.services.audit import log_action_tx
from .operations import digest


def wants(text):
    return bool(re.search(r'FCS-[A-Za-z0-9]+', text) and re.search(r'关联已有费用|全部到账|退出本月核对', text))


def source_snapshot(source):
    return dict(source_id=source.source_id, sha256=source.sha256, version=source.version,
                parsed_hash=digest(source.parsed), decisions_hash=digest(source.decisions))


async def evidence(db, cycle, source, spec, *, lock=False, reviews=None):
    from .expense_payment import load, snapshot
    base = source_snapshot(source)
    if spec['action'] == 'linked_expenses':
        if source.kind != 'note' or source.parsed.get('facts'):
            raise ValueError('仅可将没有新费用明细的补充说明关联到已有支出；有金额明细的原件请逐笔核对。')
        rows = await load(db, cycle, spec['expense_ids'], lock=lock)
        return dict(source=base, expenses=snapshot(rows)), rows
    if spec['action'] == 'receipt':
        if source.kind != 'ota':
            raise ValueError('到账确认必须明确指定本月平台账单。')
        if reviews is None:
            from .reconciled_sources import _ota_review
            from sqlalchemy.orm import undefer
            doc = await db.scalar(select(MonthlyCloseDocument).options(undefer(MonthlyCloseDocument.content))
                .where(MonthlyCloseDocument.document_id == source.document_id))
            from hashlib import sha256
            review = await _ota_review(db, cycle, source, doc) if (doc and doc.is_active
                and doc.cycle_id == cycle.cycle_id and doc.sha256 == source.sha256
                and sha256(doc.content).hexdigest() == source.sha256) else None
        else:
            review = reviews.get(source.source_id)
        if not review or review.get('kind') != 'ota' or review.get('state') != 'matched':
            raise ValueError('平台原件、已复核批次和当前订单尚未核对一致，请先处理平台差异。')
        amount = sum((Decimal(f['amount']) for f in source.parsed['facts']), Decimal(0))
        if Decimal(spec['amount']) != amount or amount <= 0:
            raise ValueError(f'指定到账金额与这份账单净额 {amount:.2f} 元不一致，请核实。')
        return dict(source=base, review=review, amount=f'{amount:.2f}'), []
    if spec['action'] == 'withdraw_cleaning':
        if source.kind != 'cleaning' or not source.document_id:
            raise ValueError('此操作仅用于退出已被新版本替代的保洁原表。')
        query = select(MonthlyCloseDocument).where(MonthlyCloseDocument.document_id == source.document_id)
        doc = await db.scalar(query.with_for_update() if lock else query)
        if not doc or not doc.is_active or doc.cycle_id != cycle.cycle_id or doc.source_type != 'cleaning_statement':
            raise ValueError('原表已退出或已变化，请刷新后核对。')
        replacement = await db.scalar(select(MonthlyCloseDocument).where(
            MonthlyCloseDocument.cycle_id == cycle.cycle_id,
            MonthlyCloseDocument.document_id == spec['replacement_document_id'],
            MonthlyCloseDocument.is_active.is_(True), MonthlyCloseDocument.source_type == 'cleaning_statement'))
        if not replacement or replacement.document_id == doc.document_id:
            raise ValueError('替代原表不存在或不是本月有效保洁表。')
        return dict(source=base, document_sha=doc.sha256, replacement_sha=replacement.sha256,
                    replacement_id=replacement.document_id), []
    raise ValueError('不支持的来源确认操作。')


async def propose(db, cycle, actor, text, run_id):
    from .service import load_sources, actor_id, require_admin
    from app.services.monthly_close.workflow import require_cycle_writable
    await require_admin(db, actor)
    await require_cycle_writable(db, cycle)
    source_match = re.fullmatch(r'资料\s+(FCS-[A-Za-z0-9]+)\s+确认全部关联已有费用\s+((?:EXP-[A-Za-z0-9-]+[\s、,，]*)+)', text.strip())
    receipt = re.fullmatch(r'资料\s+(FCS-[A-Za-z0-9]+)\s+确认全部到账\s+(\d+(?:\.\d{1,2})?)\s*元', text.strip())
    withdraw = re.fullmatch(r'资料\s+(FCS-[A-Za-z0-9]+)\s+退出本月核对，保留原件和历史记录，改用\s+(FCS-[A-Za-z0-9]+)', text.strip())
    match = source_match or receipt or withdraw
    if not match:
        raise ValueError('请明确资料编号及操作：确认全部关联已有费用 EXP-编号；确认全部到账 金额元；或退出本月核对，保留原件和历史记录，改用 FCS-新版本编号。')
    sources = {s.source_id:s for s in await load_sources(db, cycle.cycle_id)}
    source = sources.get(match[1])
    if not source:
        raise ValueError('所选资料不属于本月有效原件，请刷新后重新选择。')
    if source_match:
        spec = dict(action='linked_expenses', expense_ids=sorted(set(re.findall(r'EXP-[A-Za-z0-9-]+', match[2]))))
        if len(spec['expense_ids']) > 20:
            raise ValueError('一次最多关联 20 笔已有费用。')
    elif receipt:
        spec = dict(action='receipt', amount=format(Decimal(match[2]), '.2f'))
    else:
        replacement = sources.get(match[2])
        if not replacement or replacement.kind != 'cleaning':
            raise ValueError('请选择本月有效的新版本保洁原表。')
        spec = dict(action='withdraw_cleaning', replacement_document_id=replacement.document_id)
    current, rows = await evidence(db, cycle, source, spec)
    details = [dict(label='确认范围', value=f'{cycle.billing_month} · {source.filename} · {source.source_id}')]
    if rows:
        details.append(dict(label='整份说明已处理', value='确认这份补充说明全部对应以下已有支出；如还有未记费用，请取消本方案并补充说明。'))
        details += [dict(label=f'{r.expense_date} · {r.expense_id} · {r.amount:.2f} 元',
            value=f'{r.description}；承担方：{r.payer.value}；实际付款方：{r.paid_by.value if r.paid_by else "未登记"}；关联说明，不新增支出。') for r in rows]
    elif receipt:
        details.append(dict(label='人工确认平台到账', value=f'{spec["amount"]} 元已全部收到。仅保存管理员确认；不补造到账日期、账户、银行流水或付款记录。'))
    else:
        details.append(dict(label='退出旧版本', value=f'改用 {replacement.filename} · {replacement.source_id}；保留旧原件、历史打扫记录和已有费用，可重新归档使用。'))
    proposal = FinancialCaseProposal(proposal_id='FCP-'+uuid4().hex[:20].upper(), cycle_id=cycle.cycle_id,
        run_id=run_id, created_by=actor_id(actor), status='pending', snapshot_hash=digest(current),
        payload=dict(type='source_acknowledgement', source_id=source.source_id, spec=spec, details=details,
                     selection={'months':[cycle.billing_month]}, owner_ids=sorted({r.owner_id for r in rows if r.owner_id})))
    db.add(proposal)
    await db.flush()
    return proposal


async def execute(db, cycle, actor, proposal):
    from .service import load_sources, actor_id
    sources = {s.source_id:s for s in await load_sources(db, cycle.cycle_id)}
    source = sources.get(proposal.payload['source_id'])
    if not source:
        raise ValueError('资料已退出本月核对，请重新生成方案。')
    spec = proposal.payload['spec']
    current, _ = await evidence(db, cycle, source, spec, lock=True)
    if digest(current) != proposal.snapshot_hash:
        raise ValueError('原件或关联账目已变化，请重新核对方案。')
    if spec['action'] == 'withdraw_cleaning':
        from app.services.monthly_close.documents import archive_document
        await archive_document(db, cycle, document_id=source.document_id, user_id=actor_id(actor), commit=False)
    proposal.status = 'completed'
    proposal.result = dict(kind='source_acknowledgement', count=1, details=proposal.payload['details'],
        action=spec['action'], confirmed_at=datetime.now(timezone.utc).isoformat(), confirmed_by=actor_id(actor))
    await log_action_tx(db, actor_id(actor), 'financial_case.source_acknowledged', 'financial_case_proposal',
                       proposal.proposal_id, after_data=proposal.result)
    await db.flush()


async def enrich_reviews(db, cycle, sources, reviews):
    by_id = {s.source_id:s for s in sources}
    proposals = await db.scalars(select(FinancialCaseProposal).where(
        FinancialCaseProposal.cycle_id == cycle.cycle_id, FinancialCaseProposal.status == 'completed'))
    for proposal in proposals:
        if proposal.payload.get('type') != 'source_acknowledgement':
            continue
        source = by_id.get(proposal.payload['source_id'])
        spec = proposal.payload['spec']
        if not source or spec['action'] == 'withdraw_cleaning':
            continue
        try:
            # Strip the enrichment from prior acknowledgements when replaying proof.
            clean_reviews = {k:{a:b for a,b in v.items() if a != 'receipt_confirmation'} for k,v in reviews.items()}
            current, _ = await evidence(db, cycle, source, spec, reviews=clean_reviews)
        except ValueError:
            continue
        if digest(current) != proposal.snapshot_hash:
            continue
        if spec['action'] == 'linked_expenses':
            reviews[source.source_id] = dict(kind='linked_expenses', state='matched',
                expense_ids=spec['expense_ids'], proposal_id=proposal.proposal_id)
        else:
            reviews[source.source_id]['receipt_confirmation'] = dict(amount=spec['amount'],
                proposal_id=proposal.proposal_id, confirmed_by=proposal.result['confirmed_by'],
                confirmed_at=proposal.result['confirmed_at'])
