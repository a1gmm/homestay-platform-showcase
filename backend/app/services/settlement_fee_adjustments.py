"""Validate and project immutable fee amendments without changing old bills."""
from decimal import Decimal
from fastapi import HTTPException
from sqlalchemy import select
from app.models.expense import Expense, ExpensePayer
from app.models.settlement_fee_adjustment import SettlementFeeAdjustment
from app.models.financial_case import FinancialCaseProposal


async def load_fee_adjustments(db, settlement):
    records = list(await db.scalars(select(SettlementFeeAdjustment).where(
        SettlementFeeAdjustment.settlement_id == settlement.settlement_id).order_by(SettlementFeeAdjustment.adjustment_id)))
    lines = {i.fee_adjustment_id: i for i in settlement.items if i.fee_adjustment_id}
    if set(lines) != {r.adjustment_id for r in records}:
        raise HTTPException(409, '账单费用更正记录不完整，请核实。')
    items = {i.item_id:i for i in settlement.items}
    result = []
    for record in records:
        original = await db.get(Expense, record.original_expense_id)
        replacement = await db.get(Expense, record.replacement_expense_id)
        proposal = await db.get(FinancialCaseProposal, record.proposal_id)
        item = items.get(record.original_item_id)
        line = lines[record.adjustment_id]
        entries = item.cost_share_breakdown if item else []
        entry = entries[record.entry_index] if 0 <= record.entry_index < len(entries) else {}
        delta = record.after_amount-record.before_amount
        key = f'{original.order_id}:{original.room_id}:{original.category.value}' if original else ''
        valid = (item and original and replacement and proposal and proposal.status == 'completed'
            and proposal.payload.get('type') == 'settlement_fee_correction' and proposal.created_by == record.actor_id
            and original.expense_id in proposal.payload.get('expense_ids', [])
            and original.is_deleted and not replacement.is_deleted
            and original.amount == record.before_amount and replacement.amount == record.after_amount
            and original.payer == replacement.payer == ExpensePayer.owner
            and original.owner_id == replacement.owner_id == settlement.owner_id
            and original.room_id == replacement.room_id == item.room_id == line.room_id
            and original.order_id == replacement.order_id
            and original.expense_date == replacement.expense_date
            and original.expense_date.strftime('%Y-%m') == settlement.billing_month
            and original.category.value == replacement.category.value == 'cleaning'
            and (entry.get('expense_id') == original.expense_id or entry.get('service_fee_key') == key)
            and Decimal(str(entry.get('amount') or 0)) == Decimal(str(entry.get('owner_amount') or 0)) == record.before_amount
            and line.owner_expenses == delta and line.owner_net_amount == -delta
            and line.net_revenue == line.revenue == line.commission == line.order_count == line.externally_settled_income == 0
            and len(line.cost_share_breakdown) == 1
            and line.cost_share_breakdown[0].get('expense_id') == replacement.expense_id
            and line.cost_share_breakdown[0].get('category') == 'cleaning'
            and Decimal(str(line.cost_share_breakdown[0].get('amount') or 0)) == delta
            and Decimal(str(line.cost_share_breakdown[0].get('owner_amount') or 0)) == delta)
        if not valid:
            raise HTTPException(409, '账单费用更正与原始依据不一致，请核实后导出。')
        result.append((record, original, replacement))
    return result
