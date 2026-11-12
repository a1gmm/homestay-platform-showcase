"""Typed, explicit order facts accepted through the existing approval executor.

Receipt confirmation is evidence of the administrator's answer. It deliberately
does not create a dated Payment, claim bank verification, or alter cash reports.
"""
from decimal import Decimal
import re

from app.models.order import BookingType, Channel, OrderStatus
from app.models.expense import ExpensePayer
from app.models.refund import RefundReason

KINDS = {'correct_receivable', 'confirm_receipt', 'owner_costs'}
OFFLINE = {Channel.offline, Channel.self_acquired, Channel.walk_in, Channel.private, Channel.direct}
PRICE = r'(?:应收(?:房费)?|房费)\s*(?:改为|改成|修正为|更正为)\s*(\d+(?:\.\d{1,2})?)\s*元'


def wants(text):
    return bool(re.search(r'ORD-[A-Za-z0-9-]+', text, re.I) and re.search(
        r'应收.*(?:改|修正|更正)|房费.*(?:改为|改成)|足额收到|'
        r'(?:改为|改成).*(?:房东|业主)自住|全部费用.*(?:房东|业主|公司)承担', text))


def plan(ctx, text):
    from .operations import select_orders
    # Deliberately require an unambiguous affirmative sentence. Incomplete,
    # mixed operations and quoted/hypothetical facts remain questions.
    if re.search(r'不|没|未|待核实|假如|假设|如果|举例|还是|或者|是否|退款|押金|[？?“”「」`]', text):
        raise ValueError('请分别说明已确认的应收房费、实收金额或费用承担；含未确认、退款或多个备选值的说明不会直接记账。')
    command = re.sub(r'ORD-[A-Za-z0-9-]+', '', text, flags=re.I)
    command = re.sub(r'订单|[\s、,，。;；]', '', command)
    allowed = (r'(?:应收(?:房费)?|房费)(?:改为|改成|修正为|更正为)\d+(?:\.\d{1,2})?元'
        r'|已按应收房费足额收到|(?:(?:改为|改成)(?:房东|业主)自住)?全部费用由(?:房东|业主)承担'
        r'|(?:改为|改成)公司员工入住全部费用由公司承担')
    if not re.fullmatch(allowed, command):
        raise ValueError('这段说明含有未能确定的处理内容，请分别提供订单编号及单一明确事实，再查看方案。')
    selected = select_orders(ctx, text)
    ids = set(re.findall(r'ORD-[A-Za-z0-9-]+',text,re.I))
    if not selected or len(ids) != len(selected):
        raise ValueError('请提供当前月份的准确订单编号；部分订单未找到时不会只处理其余订单。')
    if any(o.is_deleted or o.order_status in (OrderStatus.cancelled, OrderStatus.rescheduled) for o in selected):
        raise ValueError('已取消、改期或作废订单不能更正应收、确认实收或承担费用；请核实有效订单。')
    prices = re.findall(PRICE, text)
    receipt = '已按应收房费足额收到' in text
    owner = bool(re.search(r'全部费用由(?:房东|业主)承担',text))
    staff = bool(re.search(r'(?:改为|改成)公司员工入住', text))
    if sum((bool(prices), receipt, owner, staff)) != 1:
        raise ValueError('请分别提交明确的方案，例如“订单 ORD-… 应收房费改为109元”、“订单 ORD-… 已按应收房费足额收到”，或“订单 ORD-… 改为房东自住，全部费用由业主承担”。')
    operations=[]
    for order in selected:
        legs=[r for r in ctx['rooms'] if r.order_id==order.order_id]
        if not legs or any(not r.room_id or r.room_id not in ctx['room_map'] for r in legs):
            raise ValueError('订单房间尚未明确，请先核实房间及归属。')
        if owner or staff:
            self_stay=bool(re.search(r'(?:改为|改成)(?:房东|业主)自住',text)) or staff
            if self_stay and (Decimal(order.actual_price or 0)!=0 or any(Decimal(r.actual_price or 0)!=0 or Decimal(r.ota_owner_revenue or 0)!=0 for r in legs)):
                raise ValueError('自住更正仅支持已确认的零房费订单；非零金额需要先核实。')
            if any(not ctx['room_map'][r.room_id].owner_id for r in legs):
                raise ValueError('房间缺少业主归属，不能确定费用承担。')
            if any(e.order_id==order.order_id and not e.is_deleted and e.room_id not in {r.room_id for r in legs} for e in ctx['expenses']):
                raise ValueError('存在未能对应到订单房间的费用，请先核实费用归属。')
            operation = dict(kind='owner_costs',order_id=order.order_id,owner_self=self_stay)
            if staff:
                operation['cost_bearer'] = 'company'
            operations.append(operation)
            continue
        if order.channel not in OFFLINE:
            raise ValueError('这项确认只适用于线下或自来客订单，平台订单请按原始账单核对。')
        if any(r.order_id==order.order_id and not r.is_deleted and r.reason!=RefundReason.deposit_return for r in ctx['refunds']):
            raise ValueError('这笔订单有关联退款，请先逐笔核对收退款，不能直接按足额收到处理。')
        paid=sum((p.amount for p in ctx['payments'] if p.order_id==order.order_id and not p.is_deleted and not p.is_deposit),Decimal(0))
        if prices:
            if len(selected)!=1 or len(prices)!=1 or len(legs)!=1 or len(re.findall(r'\d+(?:\.\d+)?\s*元',text))!=1:
                raise ValueError('请一次明确一笔单房订单及一个应收金额，多房订单需逐房核实。')
            amount=Decimal(prices[0]).quantize(Decimal('.01'))
            if amount<=0 or amount>Decimal('99999999.99') or paid>amount:
                raise ValueError('应收金额无效或低于已登记实收，请先核对收退款。')
            if order.platform_commission_rate or (order.metadata_ or {}).get('ota_owner_revenue') or (order.metadata_ or {}).get('ota_subsidy') or legs[0].ota_owner_revenue is not None:
                raise ValueError('线下订单仍有平台定价，请先通过渠道更正核对并清除旧平台净额。')
            operations.append(dict(kind='correct_receivable',order_id=order.order_id,amount=f'{amount:.2f}'))
        else:
            amount=order.actual_price
            if amount is None or amount<=0 or sum((r.actual_price or 0 for r in legs),Decimal(0))!=amount or paid>amount:
                raise ValueError('应收房费、逐房金额或已有收款存在差异，请先核实再确认足额实收。')
            operations.append(dict(kind='confirm_receipt',order_id=order.order_id,amount=f'{amount:.2f}'))
    return operations


def detail(ctx, order, op):
    if op['kind']=='correct_receivable':
        return f"应收房费 {order.actual_price:.2f} → {op['amount']} 元，同时更新逐房与每日房价并保留人工更正；实收另行确认。"
    if op['kind']=='confirm_receipt':
        return f"管理员确认本订单累计实收 {op['amount']} 元，与应收一致。收款日期、方式和凭证待补；保存核实依据，不新增收款流水、不计入银行现金报告。"
    total=sum((e.amount for e in ctx['expenses'] if e.order_id==order.order_id and not e.is_deleted),Decimal(0))
    if op.get('cost_bearer') == 'company':
        return '改为公司员工入住，房费保持0元；退房保洁按30元由公司承担，不从房东分成扣除。关联费用按本次明确确认全部改由公司承担，需重新复核服务费。'
    nature='改为房东安排入住，房费保持0元；退房保洁按正常退房价（默认65元）由房东承担，公司员工入住才按30元由公司承担。' if op['owner_self'] else '入住性质保持。'
    return f'{nature}关联费用现为 {total:.2f} 元，全部由业主承担；业主已自行支付的费用不重复扣回。保存后需核对服务费及重新生成待确认账单。'


async def apply(db, cycle, order, op, ctx, confirmation):
    from app.services.audit import log_action_tx
    from app.services.manual_override import lock_fields
    from app.services.order_pricing import compute_daily_prices
    from app.services.reconciliation_policy import receipt_confirmation_fingerprint
    meta=dict(order.metadata_ or {})
    legs=[r for r in ctx['rooms'] if r.order_id==order.order_id]
    if op['kind']=='correct_receivable':
        amount=Decimal(op['amount'])
        leg=legs[0]
        leg.actual_price=amount
        leg.daily_prices=compute_daily_prices(leg.check_in_date,leg.check_out_date,amount)
        order.actual_price=amount
        meta=lock_fields(meta,['actual_price','daily_prices'])
        meta['price_locked']=True
        meta['receivable_correction_log']=[*(meta.get('receivable_correction_log') or []),{**confirmation,'amount':op['amount']}]
        order.metadata_=meta
        await db.flush()
        from app.api.v1.finance import _recompute_order_payment_status
        await _recompute_order_payment_status(db,order.order_id)
    elif op['kind']=='confirm_receipt':
        fact={**confirmation,'billing_month':cycle.billing_month,'amount':op['amount'],
            'paid_at':None,'method':None,'basis':'administrator_confirmed_total',
            'fingerprint':receipt_confirmation_fingerprint(order,legs,ctx['payments'],ctx['refunds'])}
        meta['receipt_confirmation']=fact
        meta['receipt_confirmation_log']=[*(meta.get('receipt_confirmation_log') or []),fact]
        order.metadata_=meta
    else:
        if op['owner_self']:
            order.booking_type=BookingType.owner_self
        party = op.get('cost_bearer', 'owner')
        meta['reconciliation_policy']={**confirmation,'service_cost_bearer':party}
        order.metadata_=meta
        for expense in ctx['expenses']:
            if expense.order_id==order.order_id and not expense.is_deleted:
                before={'payer':expense.payer.value,'owner_id':expense.owner_id}
                expense.payer=ExpensePayer(party)
                expense.owner_id=ctx['room_map'][expense.room_id].owner_id
                await log_action_tx(db,confirmation['confirmed_by'],'monthly_close.cost_allocation','expense',expense.expense_id,
                    before_data=before,after_data={'payer':party,'owner_id':expense.owner_id,'amount':str(expense.amount)},notes=confirmation['proposal_id'])
