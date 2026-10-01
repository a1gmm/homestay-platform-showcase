"""Human-readable fee evidence, with amounts taken only from settlement items."""
from collections import defaultdict
from decimal import Decimal

from fastapi import HTTPException
from sqlalchemy import select

from app.models.expense import Expense
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom


async def settlement_fee_rows(db, settlement, rooms_map, *, full_names=False):
    entries = [(item, entry) for item in settlement.items
               for entry in (item.cost_share_breakdown or [])
               if entry.get('category') != 'platform_subsidy']
    # Never silently drop an unexplained part of the confirmed deduction.
    for item in settlement.items:
        total = sum((Decimal(str(e.get('owner_amount') or 0))
                     for e in (item.cost_share_breakdown or [])
                     if e.get('category') != 'platform_subsidy'), Decimal(0))
        if total != Decimal(item.owner_expenses or 0):
            raise HTTPException(409, '结算扣费与费用明细不一致，请核实后导出。')
    ids = {e.get('expense_id') for _, e in entries if e.get('expense_id')}
    fees = {e.expense_id: e for e in (await db.scalars(
        select(Expense).where(Expense.expense_id.in_(ids)))).all()} if ids else {}
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
    for item, entry in entries:
        amount = Decimal(str(entry.get('owner_amount') or 0))
        fee = fees.get(entry.get('expense_id'))
        # Zero-valued source-confirmed waivers explain count differences. Do not
        # include company-only zero allocations or self-use guest identities.
        waiver = fee and Decimal(fee.amount) == 0 and '不计费' in fee.description
        if not amount and not waiver:
            continue
        order = orders.get(fee.order_id) if fee else None
        if order and (order.booking_type.value == 'owner_self' or order.channel.value == 'self_used'):
            if amount:
                raise HTTPException(409, '自住费用仍计入业主扣费，请核实后导出。')
            continue
        guest = order.guest_name or '' if order else ''
        if guest and not full_names:
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
        if fee and (fee.is_deleted or Decimal(fee.amount) != Decimal(str(entry.get('amount') or 0))):
            explanation += '；当前原费用已变化，本表仍列结算时扣费，需核实更正记录'
        rows.append(dict(room=room_name, guest=guest, date=str(fee.expense_date) if fee else '',
                         check_in=str(start), check_out=str(end), category=entry.get('category', ''),
                         description=fee.description if fee else '', amount=amount,
                         explanation=explanation))
    return sorted(rows, key=lambda r: (r['room'], r['date'], r['category'], r['guest']))
