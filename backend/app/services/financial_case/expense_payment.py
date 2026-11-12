"""Actor-confirmed payment-party correction of explicitly named existing expenses."""
import re
from uuid import uuid4
from sqlalchemy import select
from app.models.expense import Expense, ExpensePayer
from app.models.financial_case import FinancialCaseProposal
from app.services.audit import log_action_tx
from .operations import digest


def wants(text):
    return bool(re.search(r'EXP-[A-Za-z0-9-]+', text) and re.search(r'付款人|付款方', text))


async def load(db, cycle, ids, *, lock=False):
    query = select(Expense).where(Expense.expense_id.in_(ids)).order_by(Expense.expense_id).execution_options(populate_existing=True)
    rows = list(await db.scalars(query.with_for_update() if lock else query))
    if len(rows) != len(ids) or any(row.is_deleted or row.expense_date.strftime('%Y-%m') != cycle.billing_month for row in rows):
        raise ValueError('所选费用不存在、已作废或不属于本月，请重新核对具体费用编号。')
    if any(row.is_service_fee for row in rows):
        raise ValueError('自动服务费须通过对应服务费流程核实付款，本次不修改。')
    if any(row.payer == ExpensePayer.owner and not row.owner_id for row in rows):
        raise ValueError('业主承担的费用尚未关联业主，请先核实归属。')
    return rows


def snapshot(rows):
    return digest([{column.key: getattr(row, column.key) for column in row.__table__.columns} for row in rows])


async def propose(db, cycle, actor, text, run_id):
    from .service import actor_id
    from app.services.monthly_close.semantic_agent import month_scope_question
    ids = sorted(set(re.findall(r'EXP-[A-Za-z0-9-]+', text)))
    # Deliberately bounded syntax: no inferred IDs, amounts, dates or payer changes.
    match = re.fullmatch(r'(?:费用\s*)?(?:EXP-[A-Za-z0-9-]+[\s、,，]*)+实际付款(?:人|方)(?:改为|补记为)(公司|业主)[。！!\s]*', text.strip())
    if not match or not ids or len(ids) > 100:
        raise ValueError('请明确已有费用编号和实际付款人，例如“费用 EXP-编号 实际付款人补记为公司”。只补付款人，原金额、承担方和付款日期保持。')
    question = month_scope_question(cycle.billing_month, text, strict=True)
    if question:
        raise ValueError(question)
    rows = await load(db, cycle, ids)
    party = 'company' if match[1] == '公司' else 'owner'
    changed = [row for row in rows if row.paid_by != ExpensePayer(party)]
    if not changed:
        raise ValueError('这些费用已经登记为该付款人，无需再次修改。')
    details = [dict(label=f'{row.expense_date} · {row.expense_id} · {row.amount:.2f} 元 · {row.description}',
                    value=f'实际付款人：{ {"company":"公司","owner":"业主"}.get(row.paid_by.value if row.paid_by else None,"未登记") } → {match[1]}；原金额、费用承担方及付款日期保持。') for row in changed]
    operations = [dict(kind='expense_payment', expense_id=row.expense_id, paid_by=party) for row in changed]
    proposal = FinancialCaseProposal(proposal_id='FCP-'+uuid4().hex[:20].upper(), cycle_id=cycle.cycle_id, run_id=run_id,
        created_by=actor_id(actor), status='pending', snapshot_hash=snapshot(rows),
        payload=dict(type='expense_payment', expense_ids=ids, operations=operations,
            owner_ids=sorted({row.owner_id for row in rows if row.owner_id}), selection={'months':[cycle.billing_month]}, details=details))
    db.add(proposal)
    await db.flush()
    return proposal


async def execute(db, cycle, actor, proposal):
    from .service import actor_id
    from app.services.monthly_close.finalization import invalidate_monthly_close_subject
    rows = await load(db, cycle, proposal.payload['expense_ids'], lock=True)
    if snapshot(rows) != proposal.snapshot_hash:
        raise ValueError('费用在方案生成后已变化，请重新核对方案。')
    by_id = {row.expense_id: row for row in rows}
    for op in proposal.payload['operations']:
        if op.get('kind') != 'expense_payment' or op.get('paid_by') not in ('company', 'owner'):
            raise ValueError('不支持的费用付款人更正。')
        row = by_id[op['expense_id']]
        before = {'paid_by': row.paid_by.value if row.paid_by else None}
        row.paid_by = ExpensePayer(op['paid_by'])
        await log_action_tx(db, actor_id(actor), 'monthly_close.expense_payment_corrected', 'expense', row.expense_id,
            before_data=before, after_data={'paid_by': op['paid_by']}, notes=proposal.proposal_id)
        await invalidate_monthly_close_subject(db, cycle_id=cycle.cycle_id, subject_type='expense', subject_id=row.expense_id,
            event_id=f'{proposal.proposal_id}:{row.expense_id}', cause='管理员确认实际付款人', actor_id=actor_id(actor), evidence_hash=digest(op))
    await db.flush()
    proposal.status = 'completed'
    proposal.result = dict(kind='business_correction', count=len(proposal.payload['operations']), amount='0.00', details=proposal.payload['details'])
    await log_action_tx(db, actor_id(actor), 'monthly_close.expense_payment_completed', 'financial_case_proposal', proposal.proposal_id, after_data=proposal.result)
