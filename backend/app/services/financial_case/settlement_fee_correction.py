"""Bounded, previewed 30 -> 65 correction of owner-guest checkout cleaning.

Confirmed bills are never reopened or overwritten. Original expense rows and
bill snapshots survive; a replacement ledger fee and an immutable delta are
created atomically. Paid bills and completed months remain locked.
"""
import copy
import re
from datetime import datetime, timezone
from decimal import Decimal
from uuid import uuid4
from sqlalchemy import select, inspect
from sqlalchemy.orm import selectinload
from app.models.expense import Expense, ExpenseCategory, ExpensePayer
from app.models.financial_case import FinancialCaseProposal
from app.models.order import Order, OrderStatus, is_owner_self_order
from app.models.settlement import OwnerSettlement, OwnerSettlementItem, SettlementStatus
from app.models.settlement_fee_adjustment import SettlementFeeAdjustment
from app.services.audit import log_action_tx
from app.services.reconciliation_policy import confirmed_cost_bearer, checkout_cleaning_amount
from app.services.service_fees import get_service_fees
from app.services.service_fee_ledger import checkout_service_fee_identity_clause, lock_owner_service_fee_ledger
from .operations import digest


def wants(text):
    return bool(re.search(r'自住|房东|业主', text) and '保洁' in text and re.search(r'更正|改为|改成', text))


async def load(db, cycle, ids, *, lock=False):
    query = select(Expense).where(Expense.expense_id.in_(ids)).order_by(Expense.expense_id).execution_options(populate_existing=True)
    fees = list(await db.scalars(query.with_for_update() if lock else query))
    if len(fees) != len(ids) or any(e.is_deleted or e.expense_date.strftime('%Y-%m') != cycle.billing_month for e in fees):
        raise ValueError('费用不存在、已更正或不属于本月，请重新列出具体费用。')
    config = await get_service_fees(db)
    result = []
    for fee in fees:
        order = await db.scalar(select(Order).where(Order.order_id == fee.order_id).execution_options(populate_existing=True))
        if (not order or order.is_deleted or order.order_status in (OrderStatus.cancelled, OrderStatus.rescheduled)
            or not is_owner_self_order(order) or confirmed_cost_bearer(order.metadata_) != ExpensePayer.owner
            or fee.category != ExpenseCategory.cleaning or not fee.is_service_fee
            or fee.payer != ExpensePayer.owner or fee.paid_by == ExpensePayer.owner
            or fee.amount != Decimal('30') or checkout_cleaning_amount(order, config) != Decimal('65')):
            raise ValueError('仅支持已明确房东承担、原30元的自住退房保洁更正为65元；其他情况请先核实。')
        peers = list(await db.scalars(select(Expense.expense_id).where(
            Expense.order_id == fee.order_id, Expense.room_id == fee.room_id,
            Expense.category == ExpenseCategory.cleaning, Expense.is_deleted.is_(False),
            checkout_service_fee_identity_clause())))
        if peers != [fee.expense_id]:
            raise ValueError('保洁记录不是唯一退房保洁，请先核实重复或续住费用。')
        bills = list(await db.scalars(select(OwnerSettlement).where(
            OwnerSettlement.owner_id == fee.owner_id, OwnerSettlement.billing_month == cycle.billing_month)
            .options(selectinload(OwnerSettlement.items)).execution_options(populate_existing=True)))
        if len(bills) != 1 or bills[0].status != SettlementStatus.confirmed or bills[0].payment_date is not None:
            raise ValueError('只支持一份已确认且尚未登记打款的账单；待确认账单请通过服务费复核，已打款账单需另行处理。')
        bill = bills[0]
        key = f'{fee.order_id}:{fee.room_id}:{fee.category.value}'
        entries = [(item, idx, entry) for item in bill.items for idx, entry in enumerate(item.cost_share_breakdown or [])
                   if entry.get('expense_id') == fee.expense_id or entry.get('service_fee_key') == key]
        if len(entries) != 1:
            raise ValueError('原账单未能唯一对应这笔费用，请先核实原始扣费依据。')
        item, index, entry = entries[0]
        if (item.fee_adjustment_id or item.sponsorship_adjustment_id or item.room_id != fee.room_id
            or entry.get('category') != 'cleaning' or Decimal(str(entry.get('amount'))) != fee.amount
            or Decimal(str(entry.get('owner_amount'))) != fee.amount or Decimal(str(entry.get('share_percent'))) != 1):
            raise ValueError('原账单金额或分摊规则不适用本次更正，请先逐笔核实。')
        result.append((fee, order, bill, item, index, entry))
    return result


def snapshot(rows):
    def columns(row):
        return {column.key: getattr(row, column.key) for column in inspect(type(row)).column_attrs}
    return digest([dict(fee=columns(fee), order=columns(order), bill=columns(bill),
                        items=[columns(i) for i in sorted(bill.items, key=lambda i: i.item_id)])
                   for fee, order, bill, *_ in rows])


async def propose(db, cycle, actor, text, run_id):
    from .service import actor_id
    match = re.fullmatch(r'更正房东自住保洁费\s*((?:EXP-[A-Za-z0-9-]+[\s、,，]*)+)为65元[。\s]*', text.strip())
    if not match:
        raise ValueError('请先明确具体费用，例如“更正房东自住保洁费 EXP-费用编号 为65元”。只有已确认房东承担的退房保洁适用；公司员工入住按30元由公司承担。')
    ids = sorted(set(re.findall(r'EXP-[A-Za-z0-9-]+', match[1])))
    if not ids or len(ids) > 20:
        raise ValueError('请一次核对1至20笔明确的费用。')
    rows = await load(db, cycle, ids)
    details = [dict(label=f'{fee.room_id} · {fee.expense_date} · {fee.expense_id}',
                    value=f'房东安排入住：保洁30.00 → 65.00元，增加业主扣费35.00元；账单{bill.settlement_id}应付减少35.00元。原费用和原账单保留，另列更正记录，不登记付款。')
               for fee, _, bill, *_ in rows]
    proposal = FinancialCaseProposal(proposal_id='FCP-'+uuid4().hex[:20].upper(), cycle_id=cycle.cycle_id,
        run_id=run_id, created_by=actor_id(actor), status='pending', snapshot_hash=snapshot(rows),
        payload=dict(type='settlement_fee_correction', expense_ids=ids, details=details,
                     owner_ids=sorted({r[0].owner_id for r in rows}), selection={'months':[cycle.billing_month]}))
    db.add(proposal)
    await db.flush()
    return proposal


async def lock_scope(db, cycle, proposal):
    from app.services.monthly_close.financial_lock import acquire_month_financial_lock
    from .service import require_cycle_writable
    await acquire_month_financial_lock(db, cycle.billing_month)
    await require_cycle_writable(db, cycle)
    for owner_id in sorted(proposal.payload['owner_ids']):
        await lock_owner_service_fee_ledger(db, owner_id)
    await db.execute(select(OwnerSettlement).where(OwnerSettlement.billing_month == cycle.billing_month,
        OwnerSettlement.owner_id.in_(proposal.payload['owner_ids'])).order_by(OwnerSettlement.settlement_id)
        .with_for_update().execution_options(populate_existing=True))
    await db.execute(select(Order).join(Expense, Expense.order_id == Order.order_id)
        .where(Expense.expense_id.in_(proposal.payload['expense_ids'])).order_by(Order.order_id)
        .with_for_update(of=Order).execution_options(populate_existing=True))


async def execute(db, cycle, actor, proposal):
    from .service import actor_id
    rows = await load(db, cycle, proposal.payload['expense_ids'], lock=True)
    if snapshot(rows) != proposal.snapshot_hash:
        raise ValueError('费用、订单或账单在预览后已变化，请重新生成更正方案。')
    for fee, order, bill, original, index, entry in rows:
        values = {c.key: getattr(fee, c.key) for c in fee.__table__.columns
                  if c.key not in {'expense_id','created_at','updated_at','created_by','is_deleted','deleted_at','deleted_by'}}
        values.update(amount=Decimal('65'), created_by=actor_id(actor),
                      notes=f'{fee.notes or ""}\n费用更正依据：{proposal.proposal_id}；原费用{fee.expense_id}'.strip())
        replacement = Expense(expense_id='EXP-'+uuid4().hex[:12].upper(), **values)
        fee.is_deleted = True
        fee.deleted_at = datetime.now(timezone.utc)
        fee.deleted_by = actor_id(actor)
        db.add(replacement)
        await db.flush()
        adjustment = SettlementFeeAdjustment(adjustment_id='SFA-'+uuid4().hex[:20].upper(),
            settlement_id=bill.settlement_id, original_item_id=original.item_id, entry_index=index,
            original_expense_id=fee.expense_id, replacement_expense_id=replacement.expense_id,
            before_amount=Decimal('30'), after_amount=Decimal('65'), proposal_id=proposal.proposal_id,
            actor_id=actor_id(actor), reason='管理员确认：房东及其安排人员入住，退房保洁65元由房东承担；公司员工入住30元由公司承担。')
        db.add(adjustment)
        await db.flush()
        delta_entry = copy.deepcopy(entry)
        delta_entry.pop('service_fee_key', None)
        delta_entry.update(expense_id=replacement.expense_id, amount='35.00', owner_amount='35.00')
        line = OwnerSettlementItem(item_id='SI-'+uuid4().hex[:16].upper(), settlement_id=bill.settlement_id,
            room_id=fee.room_id, label='房东自住保洁更正', order_count=0, revenue=0, commission=0,
            net_revenue=0, externally_settled_income=0, share_ratio_snapshot=original.share_ratio_snapshot,
            owner_expenses=Decimal('35'), owner_net_amount=Decimal('-35'),
            fee_adjustment_id=adjustment.adjustment_id, cost_share_breakdown=[delta_entry])
        db.add(line)
        await log_action_tx(db, actor_id(actor), 'monthly_close.settlement_fee_corrected', 'settlement', bill.settlement_id,
            before_data={'expense_id':fee.expense_id,'amount':'30.00'},
            after_data={'expense_id':replacement.expense_id,'amount':'65.00','deduction_delta':'35.00','adjustment_id':adjustment.adjustment_id}, notes=proposal.proposal_id)
    await db.flush()
    proposal.status = 'completed'
    proposal.result = dict(kind='settlement_fee_correction', count=len(rows), amount=f'{Decimal(35)*len(rows):.2f}', details=proposal.payload['details'])
    for bill in {r[2].settlement_id:r[2] for r in rows}.values():
        await db.refresh(bill, ['items'])
