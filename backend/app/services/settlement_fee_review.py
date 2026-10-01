"""Read-only, deterministic fee checks. Existing amounts are never repriced.

Findings are evidence, not write commands. Missing rows use the existing planner;
normal continuation/company allocations are explanations, not blocking tasks.
"""
from collections import defaultdict
from datetime import date
from decimal import Decimal

from sqlalchemy import and_, or_, select

from app.models.expense import Expense, ExpenseCategory, ExpensePayer
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.room import Room
from app.services.reconciliation_policy import service_cost_payer
from app.services.service_fee_ledger import checkout_service_fee_identity_clause
from app.services.service_fee_reconciliation import plan_service_fee_reconciliation

LABELS = {"cleaning": "正常保洁", "laundry": "布草洗涤", "daily_supplies": "日耗品"}
REASONS = {
    "missing_room": ("入住记录缺少房间", "补全实际入住房间后重新检查。"),
    "missing_owner": ("房间未关联业主", "确认房间归属并关联业主后重新检查。"),
    "missing_dates": ("入住或退房日期缺失", "根据实际入住记录补全日期。"),
    "invalid_dates": ("入住日期晚于或等于退房日期", "核实实际入住、退房日期。"),
    "cross_room_continuation": ("续住涉及不同房间", "按实际换房日期核实各房间入住段。"),
    "duplicate_final_nodes": ("续住有多个最终退房记录", "核实重复记录，确定实际最后一段入住。"),
    "overlapping_stay_group": ("同房续住日期重叠", "核实重叠入住段，不能按重复晚数收费。"),
    "gapped_stay_group": ("续住中间有空档", "核实是否真的连续入住；分开的住宿应拆开关联。"),
    "wrong_expense_month": ("费用记账月份与最终退房月份不一致", "核对两个月是否已收费，再按实际记录更正；不要重复补收。"),
    "duplicate_service_fee": ("同一入住重复登记退房服务费", "核对原始凭据，保留实际发生的一笔，通过费用更正处理重复项。"),
}


async def review_service_fees(db, year, month):
    start = date(year, month, 1)
    end = date(year + 1, 1, 1) if month == 12 else date(year, month + 1, 1)
    # One month of stays plus their explicit continuation groups. Never infer a
    # group from a guest's name, and never inspect unrelated months in bulk.
    posted_order_ids = select(Expense.order_id).where(
        Expense.is_deleted.is_(False), Expense.expense_date >= start, Expense.expense_date < end,
        or_(Expense.is_service_fee.is_(True), Expense.category == ExpenseCategory.cleaning),
        checkout_service_fee_identity_clause())
    candidates = list((await db.execute(select(OrderRoom, Order, Room)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .outerjoin(Room, Room.room_id == OrderRoom.room_id)
        .where(Order.is_deleted.is_(False), Order.order_status != OrderStatus.cancelled,
               or_(Order.order_id.in_(posted_order_ids), and_(
               or_(OrderRoom.checked_out_at.is_not(None), Order.order_status.in_(
                   (OrderStatus.pending_checkout, OrderStatus.pending_payment, OrderStatus.completed))),
               or_(and_(OrderRoom.check_out_date >= start, OrderRoom.check_out_date < end),
                   and_(OrderRoom.check_out_date.is_(None), Order.check_out_date >= start, Order.check_out_date < end))))))).all())
    owner_ids = sorted({r.owner_id for _, _, r in candidates if r and r.owner_id})
    issues, notes = [], []
    for stay, order, room in candidates:
        reason = "missing_room" if room is None else "missing_owner" if not room.owner_id else None
        if reason:
            title, action = REASONS[reason]
            issues.append(dict(code="service_fee_" + reason, message=title,
                order_id=order.order_id, room_id=stay.room_id, next_action=action))
    for owner_id in owner_ids:
        plan = await plan_service_fee_reconciliation(db, owner_id, year, month)
        for item in plan.unresolved:
            title, action = REASONS.get(item.reason, ("服务费依据需要核实", "核实入住关系和费用原始依据后重试。"))
            issues.append(dict(code="service_fee_" + item.reason, message=title,
                order_id=item.order_id, room_id=item.room_id, next_action=action))
        for item in plan.missing:
            notes.append(dict(code="service_fee_missing", message=f"{LABELS.get(item.category.value, '服务费')}尚未入账",
                order_id=item.order_id, room_id=item.room_id, amount=item.amount,
                expected_amount=item.amount, current_amount=Decimal(0), difference=item.amount,
                next_action="生成结算方案时会按现有规则预览补录；确认结算前必须完成入账。"))
    groups = {o.stay_group_id for _, o, _ in candidates if o.stay_group_id}
    rows = candidates
    if groups:
        related = list((await db.execute(select(OrderRoom, Order, Room)
            .join(Order, Order.order_id == OrderRoom.order_id)
            .outerjoin(Room, Room.room_id == OrderRoom.room_id)
            .where(Order.is_deleted.is_(False), Order.order_status != OrderStatus.cancelled,
                   Order.stay_group_id.in_(groups)))).all())
        rows = list({r.order_room_id: (r, o, room) for r, o, room in [*candidates, *related]}.values())
    order_ids = {o.order_id for _, o, _ in rows}
    fees = list((await db.scalars(select(Expense).where(
        Expense.order_id.in_(order_ids), Expense.is_deleted.is_(False),
        or_(Expense.is_service_fee.is_(True), Expense.category == ExpenseCategory.cleaning),
        checkout_service_fee_identity_clause()))).all()) if order_ids else []
    by_pair = defaultdict(list)
    for fee in fees:
        by_pair[(fee.order_id, fee.room_id)].append(fee)
    grouped = defaultdict(list)
    for room_stay, order, room in rows:
        grouped[(order.stay_group_id or order.order_id, room_stay.room_id)].append((room_stay, order, room))
    for segments in grouped.values():
        segments.sort(key=lambda v: (v[0].check_in_date or date.min, v[0].check_out_date or date.min, v[0].order_room_id))
        final_stay, final_order, _ = segments[-1]
        final_in_month = bool(final_stay.check_out_date and start <= final_stay.check_out_date < end)
        invalid_dates = any(not s.check_in_date or not s.check_out_date or s.check_out_date <= s.check_in_date for s, _, _ in segments)
        broken_group = any(a[0].check_out_date != b[0].check_in_date for a, b in zip(segments, segments[1:]))
        if invalid_dates or broken_group:
            # A fee posted this month must not escape review just because the
            # malformed group's final checkout falls in another month.
            for stay, order, _ in segments:
                for fee in by_pair[(order.order_id, stay.room_id)]:
                    if not final_in_month and start <= fee.expense_date < end:
                        issues.append(dict(code="service_fee_posted_ambiguous_stay",
                            message="本月已登记服务费，但关联入住日期或续住关系不完整，无法确认费用月份",
                            order_id=fee.order_id, room_id=fee.room_id, expense_id=fee.expense_id,
                            amount=Decimal(fee.amount or 0),
                            next_action="核实入住日期、续住空档或重叠，再确认这笔费用应归月份；不要自动重复补收。"))
            continue  # Planner handles final-month ambiguities.
        if not final_in_month:
            for fee in by_pair[(final_order.order_id, final_stay.room_id)]:
                if start <= fee.expense_date < end:
                    issues.append(dict(code="service_fee_posted_wrong_month",
                        message=f"{LABELS.get(fee.category.value, '服务费')}记在本月，实际最终退房为{final_stay.check_out_date}，费用月份不一致",
                        order_id=fee.order_id, room_id=fee.room_id, expense_id=fee.expense_id,
                        amount=Decimal(fee.amount or 0),
                        next_action="核对实际退房日期及两个月的费用，按原始依据更正记账月份，不要在退房月份重复补收。"))
        earlier = [fee for stay, order, _ in segments[:-1]
                   for fee in by_pair[(order.order_id, stay.room_id)] if Decimal(fee.amount or 0) > 0
                   and (final_in_month or start <= fee.expense_date < end)]
        if not final_in_month and not earlier:
            continue
        if len({service_cost_payer(o) for _, o, _ in segments}) > 1:
            issues.append(dict(code="service_fee_mixed_payer", message="续住前后费用承担方不同，不能全部按最后一段收费",
                order_id=final_order.order_id, room_id=final_stay.room_id,
                next_action="核实公司接待、自住与正常经营各自的日期范围，再拆分费用承担方。"))
            continue
        for fee in earlier:
            issues.append(dict(code="service_fee_earlier_segment", message=f"连续续住的前段已有{LABELS.get(fee.category.value, '服务费')}，可能与最终退房重复收费",
                order_id=fee.order_id, room_id=fee.room_id, expense_id=fee.expense_id,
                amount=Decimal(fee.amount), current_amount=Decimal(fee.amount),
                next_action="核实前段已收费用和最终退房费用；保留实际收费依据，通过更正处理，不能再次自动补收。"))
        if not final_in_month:
            continue
        actual = by_pair[(final_order.order_id, final_stay.room_id)]
        for category, label in LABELS.items():
            matched = [f for f in actual if f.category.value == category]
            if len(matched) != 1:
                continue
            fee = matched[0]
            if not start <= fee.expense_date < end:
                continue
            nights = sum((s.check_out_date - s.check_in_date).days for s, _, _ in segments)
            reasons = []
            if len(segments) > 1:
                reasons.append(f"同房连续入住{len(segments)}段，共{nights}晚；正常退房保洁和洗涤按最终退房计一次，日耗按全部入住晚数计费")
            if segments[0][0].check_in_date < start:
                prior = sum(max(0, (min(s.check_out_date, start) - s.check_in_date).days) for s, _, _ in segments)
                reasons.append(f"含本月之前{prior}晚；费用归最终退房月份，不能只按本月收入表晚数相乘")
            if fee.payer == ExpensePayer.company:
                reasons.append("本笔由公司承担，不从业主结算中扣除")
            if Decimal(fee.amount or 0) == 0:
                reasons.append("当前已登记为0元；保留原费用依据，不按现行单价重新补收")
            if reasons and not earlier:
                notes.append(dict(code="service_fee_explained", message=f"{label}：" + "；".join(reasons),
                    order_id=fee.order_id, room_id=fee.room_id, expense_id=fee.expense_id,
                    amount=Decimal(fee.amount or 0), current_amount=Decimal(fee.amount or 0),
                    next_action="无需因为订单条数或月份不同而再次补收；如原始记录有误，应先核实再更正。"))
    # The planner sees all rooms for every owner; avoid repeating the same ambiguity.
    def unique(values):
        deduped = {(v['code'], v.get('order_id'), v.get('room_id'), v.get('expense_id'), v['message']): v for v in values}
        return [deduped[key] for key in sorted(deduped, key=lambda k: tuple(v or '' for v in k))]
    return unique(issues), unique(notes)
