"""Human-readable fee evidence, with amounts taken only from settlement items."""
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from app.models.expense import Expense, ExpensePayer
from app.models.order import Order, OrderStatus, is_owner_self_order
from app.models.order_room import OrderRoom
from app.services.reconciliation_policy import confirmed_cost_bearer


def fee_evidence_key(item, index, entry):
    return entry.get('expense_id') or (item.item_id, index)


async def load_settlement_fee_evidence(db, settlement):
    """Resolve approved business keys without modifying the settlement snapshot."""
    from app.services.settlement_fee_adjustments import load_fee_adjustments
    adjustments = await load_fee_adjustments(db, settlement)
    originals = {original.expense_id:original for _, original, _ in adjustments}
    replacements = {replacement.expense_id for _, _, replacement in adjustments}
    entries = [(item, index, entry) for item in settlement.items for index, entry in enumerate(item.cost_share_breakdown or [])]
    ids = {e['expense_id'] for _, _, e in entries if e.get('expense_id')}
    fees = {e.expense_id: e for e in (await db.scalars(
        select(Expense).where(Expense.expense_id.in_(ids)))).all()} if ids else {}
    keys = {e['service_fee_key'] for _, _, e in entries if not e.get('expense_id') and e.get('service_fee_key')}
    if not keys:
        return fees
    year, month = map(int, settlement.billing_month.split('-'))
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    candidates = await db.scalars(select(Expense).where(
        Expense.owner_id == settlement.owner_id, Expense.is_service_fee.is_(True),
        Expense.is_deleted.is_(False), Expense.expense_date >= date(year, month, 1), Expense.expense_date < end)
        .order_by(Expense.expense_date, Expense.expense_id))
    by_key = defaultdict(list)
    for fee in [f for f in candidates if f.expense_id not in replacements] + list(originals.values()):
        key = f'{fee.order_id}:{fee.room_id}:{fee.category.value}'
        if key in keys:
            by_key[key].append(fee)
    expected = defaultdict(list)
    for item, index, entry in entries:
        key = entry.get('service_fee_key') if not entry.get('expense_id') else None
        if not key:
            continue
        expected[key].append((item, index, entry))
    for key, group in expected.items():
        matches = by_key[key]
        amounts = Counter(Decimal(str(e.get('amount') or 0)) for _, _, e in group)
        if amounts != Counter(Decimal(f.amount) for f in matches):
            raise HTTPException(409, '结算服务费依据缺失或不唯一，请核实后导出。')
        # Several stayover cleans can share one order/room/category key. Prove
        # the complete multiset, then consume every source exactly once. Equal
        # source amounts are interchangeable only for identical allocations.
        for amount, count in amounts.items():
            allocations = {tuple(str(e.get(k)) for k in ('owner_amount', 'share_percent', 'rule_id', 'booking_type'))
                           for _, _, e in group if Decimal(str(e.get('amount') or 0)) == amount}
            if count > 1 and len(allocations) != 1:
                raise HTTPException(409, '结算服务费分摊依据不唯一，请核实后导出。')
        remaining = list(matches)
        for item, index, entry in group:
            fee = next(f for f in remaining if Decimal(f.amount) == Decimal(str(entry.get('amount') or 0)))
            if fee.room_id != item.room_id or fee.category.value != entry.get('category'):
                raise HTTPException(409, '结算服务费与房间不一致，请核实后导出。')
            remaining.remove(fee)
            fees[fee_evidence_key(item, index, entry)] = fee
    return fees


async def settlement_fee_rows(db, settlement, rooms_map, *, full_names=False):
    from app.services.settlement_fee_adjustments import load_fee_adjustments
    adjustments = await load_fee_adjustments(db, settlement)
    amendments = {(r.original_item_id,r.entry_index): r for r, _, _ in adjustments}
    entries = [(item, index, entry) for item in settlement.items
               for index, entry in enumerate(item.cost_share_breakdown or [])
               if entry.get('category') != 'platform_subsidy']
    # Never silently drop an unexplained part of the confirmed deduction.
    for item in settlement.items:
        total = sum((Decimal(str(e.get('owner_amount') or 0))
                     for e in (item.cost_share_breakdown or [])
                     if e.get('category') != 'platform_subsidy'), Decimal(0))
        if total != Decimal(item.owner_expenses or 0):
            raise HTTPException(409, '结算扣费与费用明细不一致，请核实后导出。')
    fees = await load_settlement_fee_evidence(db, settlement)
    order_ids = {e.order_id for e in fees.values() if e.order_id}
    orders = {o.order_id: o for o in (await db.scalars(
        select(Order).where(Order.order_id.in_(order_ids)))).all()} if order_ids else {}
    group_ids = {o.stay_group_id for o in orders.values() if o.stay_group_id}
    related = (await db.execute(select(OrderRoom, Order).join(Order)
        .where(Order.is_deleted == False, Order.order_status != OrderStatus.cancelled,
               (Order.order_id.in_(order_ids)) | (Order.stay_group_id.in_(group_ids))))).all() if orders else []
    segments = defaultdict(list)
    for room, order in related:
        segments[(order.stay_group_id or order.order_id, room.room_id)].append(room)
    rows = []
    for item, index, entry in entries:
        if item.fee_adjustment_id:
            continue  # Its validated delta is presented with the original fee below.
        amount = Decimal(str(entry.get('owner_amount') or 0))
        amendment = amendments.get((item.item_id, index))
        if amendment:
            amount += amendment.after_amount-amendment.before_amount
        fee = fees.get(fee_evidence_key(item, index, entry))
        # Zero-valued source-confirmed waivers explain count differences. Do not
        # include company-only zero allocations or self-use guest identities.
        waiver = fee and Decimal(fee.amount) == 0 and '不计费' in fee.description
        if not amount and not waiver:
            continue
        order = orders.get(fee.order_id) if fee else None
        self_stay = order is not None and is_owner_self_order(order)
        if self_stay:
            if amount and confirmed_cost_bearer(order.metadata_) != ExpensePayer.owner:
                raise HTTPException(409, '自住费用仍计入业主扣费，请核实后导出。')
            if not amount:
                continue
        guest = '业主自住' if self_stay else (order.guest_name or '' if order else '')
        if guest and not full_names and not self_stay:
            guest = guest[:1] + '**'
        room_id = item.room_id or (fee.room_id if fee else None)
        room_name = rooms_map[room_id][1] if room_id in rooms_map else (item.label or '整层费用')
        parts = sorted(segments.get((order.stay_group_id or order.order_id, room_id), []),
                       key=lambda r: (r.check_in_date, r.check_out_date)) if order else []
        contiguous = bool(parts) and all(a.check_out_date == b.check_in_date
                                        for a, b in zip(parts, parts[1:]))
        start = parts[0].check_in_date if contiguous else ''
        end = parts[-1].check_out_date if contiguous else ''
        explanation = fee.description if fee else '历史结算金额；原费用资料暂不可用'
        if contiguous and entry.get('category') == 'daily_supplies':
            nights = sum((r.check_out_date - r.check_in_date).days for r in parts)
            explanation = f'关联入住共{nights}晚；日耗在最终退房时合并计费'
            if len(parts) > 1:
                explanation += '；含连续续住的前段，不能只按本月收入明细的晚数计算'
        if waiver:
            explanation += '；按已确认原保洁表，本次正常保洁为0元'
        if self_stay:
            explanation += '；已确认由业主承担自住费用'
        if amendment:
            explanation += f'；原扣费{amendment.before_amount:.2f}元，已确认更正增加{amendment.after_amount-amendment.before_amount:.2f}元，现扣费{amendment.after_amount:.2f}元；详见费用更正记录'
        elif fee and (fee.is_deleted or Decimal(fee.amount) != Decimal(str(entry.get('amount') or 0))):
            explanation += '；当前原费用已变化，本表仍列结算时扣费，需核实更正记录'
        rows.append(dict(room=room_name, guest=guest, date=str(fee.expense_date) if fee else '',
                         check_in=str(start), check_out=str(end), category=entry.get('category', ''),
                         description=fee.description if fee else '', amount=amount,
                         explanation=explanation))
    return sorted(rows, key=lambda r: (r['room'], r['date'], r['category'], r['guest']))
