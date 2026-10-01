"""Preview-only business corrections, executed through actor-bound case proposals.

Amounts come from explicit user input and Decimal arithmetic, never from a
model's arithmetic. The executor re-reads every bound order/room/payment/fee,
locks all affected months and refuses edits to confirmed settlements.
"""
from __future__ import annotations
import calendar
import json
import re
from datetime import date, datetime, timezone
from decimal import Decimal
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select, or_
from app.models.order import Order, Channel, OrderStatus, StaySettlementKind
from app.models.order_room import OrderRoom
from app.models.payment import Payment
from app.models.refund import Refund, RefundReason
from app.models.expense import Expense, ExpensePayer
from app.models.room import Room
from app.models.financial_case import FinancialCaseProposal
from app.services.audit import log_action_tx
from app.services.reconciliation_policy import order_acceptance_fingerprint

KINDS = {'company_reception', 'accept_current', 'correct_order', 'reassign_payment', 'transfer_allocation'}


def normalize_instruction(text):
    digits = {'一': 1, '二': 2, '两': 2, '三': 3, '四': 4, '五': 5, '六': 6, '七': 7, '八': 8, '九': 9, '十': 10}
    return re.sub(r'([前后])\s*([一二两三四五六七八九十])\s*晚', lambda m: f'{m[1]}{digits[m[2]]}晚', text)


def wants_operation(text):
    text = normalize_instruction(text)
    if re.search(r'排除|不算|只看|合计|多少钱|费用多少|为什么',text) and not re.search(r'改为|改成|修正|重新关联|公司承担|公司全担|不用补房费',text):
        return False
    return bool(re.search(r'公司接待|接待.*公司承担|按(?:照)?(?:现在|当前|现有)?系统(?:.*(?:走|为准)|[\s，。！!]*$)|保留系统.*(?:金额|记录)|换房.*(?:分摊|分配|前|后)|前\s*\d+\s*晚.*后\s*\d+\s*晚|(?:收款|付款).*(?:转关联|重新关联)|(?:渠道|净额|净收入).*(?:改为|改成|修正为)|(?:改为|改成).*(?:携程|美团|线下|自来客)', text))


def digest(value):
    return sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,default=str,separators=(',',':')).encode()).hexdigest()


async def load_context(db, cycle, *, lock=False):
    start=date.fromisoformat(cycle.billing_month+'-01')
    end=date(start.year,start.month,calendar.monthrange(start.year,start.month)[1])
    # Include cancelled originals for payment reassociation, never for revenue edits.
    query=select(Order).where(or_(
        Order.order_id.in_(select(OrderRoom.order_id).where(OrderRoom.check_out_date>=start,OrderRoom.check_out_date<=end)),
        (Order.check_out_date>=start)&(Order.check_out_date<=end))).order_by(Order.order_id)
    if lock:query=query.with_for_update()
    orders=list(await db.scalars(query.execution_options(populate_existing=True)))
    ids=[o.order_id for o in orders]
    async def rows(model, key):
        q=select(model).where(model.order_id.in_(ids)).order_by(key)
        if lock:q=q.with_for_update()
        return list(await db.scalars(q.execution_options(populate_existing=True)))
    rooms=await rows(OrderRoom,OrderRoom.order_room_id)
    payments=await rows(Payment,Payment.payment_id)
    expenses=await rows(Expense,Expense.expense_id)
    refunds=await rows(Refund,Refund.refund_id)
    from app.models.managed_stay_group import ManagedStayGroup, ManagedStayGroupKind
    managed=set(await db.scalars(select(ManagedStayGroup.stay_group_id).where(ManagedStayGroup.kind==ManagedStayGroupKind.managed_split)))
    room_query=select(Room).where(Room.room_id.in_([r.room_id for r in rooms])).order_by(Room.room_id)
    if lock:room_query=room_query.with_for_update()
    room_map={r.room_id:r for r in await db.scalars(room_query.execution_options(populate_existing=True))}
    return dict(orders=orders,rooms=rooms,payments=payments,expenses=expenses,refunds=refunds,room_map=room_map,managed=managed)


def snapshot(ctx, ids):
    # Capture all columns, including prices, deletion state, ledger lineage and user confirmations.
    def canonical(value):
        if isinstance(value,Decimal):return format(value.normalize(),'f')
        if isinstance(value,datetime) and value.tzinfo:return value.astimezone(timezone.utc).isoformat()
        return value
    def values(row):return {c.key:canonical(getattr(row,c.key if c.key!='metadata' else 'metadata_')) for c in row.__table__.columns}
    data={k:[values(r) for r in ctx[k] if r.order_id in ids] for k in ('orders','rooms','payments','expenses','refunds')}
    room_ids={r.room_id for r in ctx['rooms'] if r.order_id in ids}
    data['room_ownership']=[values(ctx['room_map'][key]) for key in sorted(room_ids) if key in ctx['room_map']]
    return digest(data)


def select_orders(ctx, text, previous_ids=()):
    explicit=set(re.findall(r'ORD-[A-Za-z0-9-]+',text,re.I))
    if explicit:return [o for o in ctx['orders'] if o.order_id.upper() in {i.upper() for i in explicit}]
    matches=[]
    dates=re.findall(r'20\d{2}-\d{2}-\d{2}',text)
    # Prices and ISO dates are not room identifiers. When a guest and a room
    # are both specified, both must match the same order.
    identity_text=re.sub(r'20\d{2}-\d{2}-\d{2}', '', text)
    identity_text=re.sub(r'(?:净额|净收入|房费)\s*(?:(?:改为|改成|修正为|是|为|[:：])\s*)?\d+(?:\.\d+)?\s*元?', '', identity_text)
    room_names=set(re.findall(r'(?<!\d)\d{3,5}(?!\d)',identity_text))
    guest_names={o.guest_name for o in ctx['orders'] if o.guest_name and len(o.guest_name)>=2 and o.guest_name in text}
    guest_names={name for name in guest_names if not any(name != other and name in other for other in guest_names)}
    for o in ctx['orders']:
        rooms=[r for r in ctx['rooms'] if r.order_id==o.order_id]
        by_guest=o.guest_name in guest_names
        by_room=bool(room_names & {str(value) for r in rooms for value in (r.room_id, getattr(ctx['room_map'].get(r.room_id), 'room_name', ''))})
        if not (guest_names or room_names):continue
        if guest_names and not by_guest:continue
        if room_names and not by_room:continue
        if dates and not any(str(r.check_in_date) in dates or str(r.check_out_date) in dates for r in rooms):continue
        matches.append(o)
    if matches:return matches
    if previous_ids and not (guest_names or room_names or dates) and re.search(r'这些|这几|这批|这笔|上述|剩下|剩余|全部|其余',text):
        return [o for o in ctx['orders'] if o.order_id in previous_ids]
    return []


def label(ctx,order):
    rooms=[r for r in ctx['rooms'] if r.order_id==order.order_id]
    return f"{order.guest_name or '未登记姓名'} · {'、'.join(getattr(ctx['room_map'].get(r.room_id), 'room_name', None) or r.room_id for r in rooms)} · {order.check_in_date} 至 {order.check_out_date}"


def plan(ctx, cycle, text, *, previous_ids=()):
    text = normalize_instruction(text)
    from app.services.monthly_close.semantic_agent import month_scope_question
    # Full stay dates identify a guest's room leg; a separately stated business
    # month still must match the current workspace, including direct commands.
    question=month_scope_question(cycle.billing_month,re.sub(r'20\d{2}-\d{2}-\d{2}', '', text),strict=True)
    if question:
        raise ValueError(question)
    if re.search(r'先别|暂不|不要执行|假如|举例|假设|不是公司接待|不由公司承担|不要.*(?:修改|修正|关联|分配|按.*系统)|不能.*(?:修改|修正|关联|分配)',text):
        raise ValueError('请说明需要实际处理的订单；本次未生成修改方案。')
    if re.search(r'除了|除外|除去|排除|不包括|不含|除.+以?外',text):
        raise ValueError('请直接列出需要处理的客人或订单，避免把排除项误选进方案。')
    # Only the no-room-compensation clause is a supported negative statement.
    # Other negations must not be mistaken for positive write instructions.
    polarity_text=re.sub(r'不(?:用|需)?补(?:偿)?房费', '', text)
    if re.search(r'不|别|勿|无需|无须|禁止',polarity_text):
        raise ValueError('这段说明包含否定或相互冲突的处理要求，请明确要采用的方案；本次未生成修改。')
    if '公司接待' in text and re.search(r'业主承担|(?:需要|要|应|必须)补(?:偿)?房费',polarity_text):
        raise ValueError('公司接待的费用承担或房费补偿说明不一致，请明确后重新生成方案。')
    selected=select_orders(ctx,text,previous_ids)
    if not selected:raise ValueError('请提供客人姓名和房号／入住日期，或先列出需要处理的订单。我会先展示具体范围与修改影响。')
    explicit_ids=set(re.findall(r'ORD-[A-Za-z0-9-]+',text,re.I))
    if explicit_ids and len(explicit_ids)!=len(selected):raise ValueError('部分订单不在当前月份，请分别在所属月份处理，不能只执行其中一部分。')
    if any(o.stay_group_id in ctx['managed'] or o.stay_settlement_kind==StaySettlementKind.company_sponsored for o in selected):
        raise ValueError('这些订单已有受控住宿段或公司补偿，请使用住宿段更正流程，不能通过普通修正覆盖。')
    operations=[]
    def add(kind,o,**fields):
        operations.append(dict(kind=kind,order_id=o.order_id,**fields))
    if re.search(r'公司接待|接待.*公司承担',text):
        if any(o.is_deleted or o.order_status==OrderStatus.cancelled for o in selected):raise ValueError('公司接待只能处理有效订单。')
        if not re.search(r'公司承担|公司全担',text):raise ValueError('请明确公司接待的保洁、洗涤和日耗费用由谁承担。')
        if not re.search(r'不(?:用|需)?补(?:偿)?房费|房费\s*0|零房费',text):raise ValueError('这批接待是否不补房费？请明确后一起生成费用承担方案。')
        if any(Decimal(o.actual_price or 0)!=0 or any(Decimal(r.ota_owner_revenue or 0)!=0 or Decimal(r.actual_price or 0)!=0 for r in ctx['rooms'] if r.order_id==o.order_id) for o in selected):
            raise ValueError('选中订单有非零房费或平台净额，不能直接作为零房费接待；请先核实收入是否确需调整。')
        for o in selected:add('company_reception',o)
    elif re.search(r'前\s*\d+\s*晚.*后\s*\d+\s*晚',text):
        n=re.search(r'前\s*(\d+)\s*晚.*后\s*(\d+)\s*晚',text)
        if len(selected)!=2:raise ValueError('换房分配需要明确同一批客人的前后两笔订单。')
        legs=sorted([r for r in ctx['rooms'] if r.order_id in {o.order_id for o in selected}],key=lambda r:r.check_in_date)
        if len(legs)!=2 or legs[0].check_out_date!=legs[1].check_in_date:raise ValueError('前后房段必须连续且各对应一个房间；请先核实换房日期。')
        if [(r.check_out_date-r.check_in_date).days for r in legs]!=[int(n[1]),int(n[2])]:raise ValueError('你说的前后晚数与当前房段日期不一致，请先核实日期；本次不会擅自改入住记录。')
        if any(o.is_deleted or o.order_status==OrderStatus.cancelled for o in selected):raise ValueError('换房分配需要两笔有效订单。')
        # Net anchors are required: never invent a new room rate for a transfer.
        if any(r.ota_owner_revenue is None for r in legs):raise ValueError('换房订单缺少已核实的平台净额，请先补充两段合计净收入。')
        if any(Decimal(r.actual_price or 0)<=0 for r in legs):raise ValueError('换房房段缺少有效房费，请先核实房费后分配净收入。')
        total=sum((Decimal(r.ota_owner_revenue) for r in legs),Decimal(0))
        first=(total*Decimal(n[1])/Decimal(int(n[1])+int(n[2]))).quantize(Decimal('.01'))
        by_id={o.order_id:o for o in selected}
        for r,value in zip(legs,[first,total-first]):
            add('transfer_allocation',by_id[r.order_id],room_id=r.room_id,net=str(value),group=[r.order_id for r in legs],nights=[int(n[1]),int(n[2])])
    elif re.search(r'(?:收款|付款).*(?:转关联|重新关联)',text):
        payment_ids=set(re.findall(r'PAY-[A-Za-z0-9-]+',text,re.I))
        if len(selected)!=2:raise ValueError('请明确取消的原订单与正确订单，并指出要关联的原收款。')
        old=[o for o in selected if o.is_deleted or o.order_status==OrderStatus.cancelled]
        new=[o for o in selected if not o.is_deleted and o.order_status!=OrderStatus.cancelled]
        if len(old)!=1 or len(new)!=1:raise ValueError('收款重关联仅支持已取消原订单到有效替代订单。')
        payments=[p for p in ctx['payments'] if p.order_id==old[0].order_id and not p.is_deposit and not p.is_deleted and (not payment_ids or p.payment_id in payment_ids)]
        if len(payments)!=1:raise ValueError('原订单有多笔或没有普通房费收款，请明确需要转关联的那笔收款。')
        if any(r.order_id in {o.order_id for o in selected} and not r.is_deleted and r.reason != RefundReason.deposit_return for r in ctx['refunds']):
            raise ValueError('这两笔订单涉及房费退款，请先核对退款与原收款的对应关系，不能单独转移收款。')
        target_paid=sum((p.amount for p in ctx['payments'] if p.order_id==new[0].order_id and not p.is_deleted and not p.is_deposit), Decimal(0))
        if payments[0].amount <= 0 or new[0].actual_price is None or target_paid+payments[0].amount > new[0].actual_price:
            raise ValueError('转关联后收款会超过目标订单房费，或目标房费尚未明确，请先核对金额。')
        add('reassign_payment',old[0],payment_id=payments[0].payment_id,target_order_id=new[0].order_id)
    elif re.search(r'按(?:照)?(?:现在|当前|现有)?系统(?:.*(?:走|为准)|[\s，。！!]*$)|保留系统.*(?:金额|记录)',text):
        for o in selected:add('accept_current',o)
    else:
        channel=next((value for word,value in [('携程','ctrip'),('美团','meituan_hotel'),('线下','offline'),('自来客','self_acquired'),('抖音','douyin'),('去哪儿','qunar')] if re.search(r'(?:改为|改成|修正为)\s*'+word,text)),None)
        net=re.search(r'(?:净额|净收入)\s*(?:改为|改成|修正为)\s*(\d+(?:\.\d{1,2})?)(?![\d.])',text)
        if len(selected)!=1 or not (channel or net):raise ValueError('请明确一笔订单及要修正的渠道或净收入；我会展示当前值和新值供确认。')
        if selected[0].is_deleted or selected[0].order_status==OrderStatus.cancelled:raise ValueError('不能调整已取消订单的收入。')
        if len([r for r in ctx['rooms'] if r.order_id==selected[0].order_id])!=1:raise ValueError('多房订单需要逐房确认净收入，不能把订单合计重复放到每间房。')
        if net and (selected[0].actual_price is None or selected[0].actual_price <= 0 or any(Decimal(r.actual_price or 0)<=0 for r in ctx['rooms'] if r.order_id==selected[0].order_id)):
            raise ValueError('该订单没有有效房费，不能只改净收入；请先核实房费与平台结算依据。')
        platform=re.search(r'平台(?:订单号|单号)\s*(?:是|为|[:：])\s*([A-Za-z0-9_-]+)',text)
        net_value=net[1] if net else None
        if channel and Channel(channel)!=selected[0].channel and net_value is None:
            raise ValueError('渠道改变可能影响佣金和净收入，请同时说明这笔订单正确的净收入，例如“改为线下，净收入改为 450 元”。')
        add('correct_order',selected[0],channel=channel,net=net_value,platform_id=platform[1] if platform else None)
    return operations


def details(ctx,operations):
    from app.api.v1.export import CHANNEL_LABELS
    by_id={o.order_id:o for o in ctx['orders']};result=[]
    for op in operations:
        order=by_id[op['order_id']]
        if op['kind']=='company_reception':
            total=sum((e.amount for e in ctx['expenses'] if e.order_id==order.order_id and not e.is_deleted),Decimal(0))
            value=f'公司接待，房费保持 0 元；现有关联费用 {total:.2f} 元由公司承担，费用总额不增加，后续生成的服务费沿用此规则。'
        elif op['kind']=='accept_current':value='采用当前系统账面作为本月结算依据；不新增房费、不伪造到账或凭证。'
        elif op['kind']=='reassign_payment':value=f"将原有收款转关联到 {label(ctx,by_id[op['target_order_id']])}；金额和原收款时间保持，不新增收款。"
        elif op['kind']=='correct_order' and op.get('platform_id') and not op.get('channel') and op.get('net') is None:value=f"补充平台订单号 {op['platform_id']}，金额保持。"
        elif op['kind']=='transfer_allocation':value=f"前 {op['nights'][0]} 晚、后 {op['nights'][1]} 晚按各自房间归属；本段净收入 {op['net']} 元，前后合计保持。"
        else:
            old_channel=CHANNEL_LABELS.get(order.channel.value, '其他渠道')
            new_channel=CHANNEL_LABELS.get(op.get('channel') or order.channel.value, '其他渠道')
            value=f"渠道 {old_channel} → {new_channel}；净收入修改为 {op['net']} 元。" if op.get('net') is not None else f"渠道 {old_channel} → {new_channel}，金额不变。"
        result.append(dict(label=label(ctx,order),value=value))
    return result


def affected_months(ctx, cycle, ids):
    """Order-wide policy/pricing changes affect every checkout period."""
    return sorted({cycle.billing_month}
        | {str(row.check_out_date)[:7] for key in ('orders','rooms') for row in ctx[key] if row.order_id in ids}
        | {str(row.expense_date)[:7] for row in ctx['expenses'] if row.order_id in ids}
        | {str(row.paid_at)[:7] for row in ctx['payments'] if row.order_id in ids and not row.is_deleted})


async def propose(db,cycle,actor,text,run_id,previous_ids=(), *, query_scope=None, context_run_id=None):
    ctx=await load_context(db,cycle)
    if query_scope is not None:
        from .order_scope import validate
        from app.services.monthly_close.semantic_agent import month_scope_question
        question=month_scope_question(cycle.billing_month,text,strict=True)
        if question:
            raise ValueError(question)
        previous_ids=await validate(db,cycle,actor,query_scope,context_run_id,ctx)
        explicitly_selected=select_orders(ctx,text)
        if any(order.order_id not in previous_ids for order in explicitly_selected):
            raise ValueError('这次指定的订单超出上次查询范围，请重新查询后再生成方案。')
    operations=plan(ctx,cycle,text,previous_ids=previous_ids)
    ids=sorted({op['order_id'] for op in operations}|{op['target_order_id'] for op in operations if op.get('target_order_id')})
    # Relevant fee and payment dates participate in locks even if the stay crosses a month.
    months=affected_months(ctx,cycle,ids)
    owners=sorted({r.owner_id for key,r in ctx['room_map'].items() if r.owner_id and any(leg.room_id==key and leg.order_id in ids for leg in ctx['rooms'])})
    payload=dict(type='business_correction',operations=operations,order_ids=ids,owner_ids=owners,
                 selection={'months':months},reason=text[:1800],details=details(ctx,operations))
    if query_scope is not None:
        payload['query_scope']=query_scope
    proposal=FinancialCaseProposal(proposal_id='FCP-'+uuid4().hex[:20].upper(),cycle_id=cycle.cycle_id,
        run_id=run_id,created_by=actor['user_id'] if isinstance(actor,dict) else actor.user_id,
        snapshot_hash=snapshot(ctx,ids),payload=payload,status='pending')
    db.add(proposal);await db.flush()
    return proposal


async def execute(db,cycle,actor,proposal):
    from app.services.financial_case.service import actor_id
    from app.services.order_identity_lock import check_platform_identity_available
    from app.services.monthly_close.finalization import invalidate_monthly_close_subject
    from .service import load_sources
    sources={s.source_id:s for s in await load_sources(db,cycle.cycle_id)}
    for key,version in proposal.payload.get('source_versions',{}).items():
        if key not in sources or sources[key].version!=version:
            raise ValueError('回填原件或说明已变化，请重新生成方案。')
    ctx=await load_context(db,cycle,lock=True)
    if not set(affected_months(ctx,cycle,proposal.payload['order_ids'])).issubset(proposal.payload.get('selection',{}).get('months',[])):
        raise ValueError('方案没有覆盖全部订单所属月份，请重新生成方案后再确认。')
    if proposal.payload.get('query_scope'):
        from .order_scope import validate
        bound=proposal.payload['query_scope']
        await validate(db,cycle,actor,bound,bound['run_id'],ctx)
    if snapshot(ctx,proposal.payload['order_ids'])!=proposal.snapshot_hash:
        raise ValueError('订单、费用或收款在方案生成后变化，请重新核对方案。')
    by_id={o.order_id:o for o in ctx['orders']}
    if any(by_id[key].stay_group_id in ctx['managed'] or by_id[key].stay_settlement_kind==StaySettlementKind.company_sponsored for key in proposal.payload['order_ids']):
        raise ValueError('选中订单已进入受控住宿段或公司补偿流程，请重新核实。')
    now=datetime.now(timezone.utc).isoformat();who=actor_id(actor)
    if proposal.payload.get('source_changes'):
        from .service import apply_decisions
        await apply_decisions(db,cycle,actor,proposal.payload['source_changes'])
    for op in proposal.payload['operations']:
        if op['kind'] not in KINDS:raise ValueError('不支持的业务操作')
        order=by_id[op['order_id']];before={'metadata':order.metadata_,'channel':order.channel.value,'platform_order_id':order.platform_order_id}
        meta=dict(order.metadata_ or {})
        confirmation=dict(version=1,confirmed_by=who,confirmed_at=now,proposal_id=proposal.proposal_id,reason=proposal.payload['reason'])
        if op['kind']=='company_reception':
            meta['reconciliation_policy']={**confirmation,'service_cost_bearer':'company','room_compensation':'none'}
            order.metadata_=meta
            for expense in ctx['expenses']:
                if expense.order_id==order.order_id and not expense.is_deleted:
                    old={'payer':expense.payer.value,'owner_id':expense.owner_id,'amount':str(expense.amount)}
                    expense.payer=ExpensePayer.company
                    await log_action_tx(db,who,'monthly_close.cost_allocation','expense',expense.expense_id,before_data=old,
                        after_data={'payer':'company','amount':str(expense.amount)},notes=proposal.proposal_id)
        elif op['kind']=='accept_current':
            meta['settlement_acceptance']={**confirmation,'billing_month':cycle.billing_month,'basis':'user_confirmed_current_system_figures','snapshot':proposal.snapshot_hash,'order_fingerprint':order_acceptance_fingerprint(order,[r for r in ctx['rooms'] if r.order_id==order.order_id])}
            order.metadata_=meta
        elif op['kind']=='reassign_payment':
            payment=next(p for p in ctx['payments'] if p.payment_id==op['payment_id'])
            old={'order_id':payment.order_id,'amount':str(payment.amount),'paid_at':str(payment.paid_at)}
            payment.order_id=op['target_order_id']
            await db.flush()
            from app.api.v1.finance import _recompute_order_payment_status
            for order_id in (order.order_id, payment.order_id):
                await _recompute_order_payment_status(db, order_id)
            await log_action_tx(db,who,'monthly_close.payment_reassociated','payment',payment.payment_id,before_data=old,
                after_data={**old,'order_id':payment.order_id},notes=proposal.proposal_id)
        elif op['kind'] in ('correct_order','transfer_allocation'):
            if op.get('platform_id'):
                await check_platform_identity_available(db,op['platform_id'],order.order_id,lock=True)
                order.platform_order_id=op['platform_id']
            from app.services.manual_override import lock_fields
            if op.get('channel'):
                order.channel=Channel(op['channel'])
                meta=lock_fields(meta,['channel'])
            if op.get('net') is not None:
                leg=next(r for r in ctx['rooms'] if r.order_id==order.order_id)
                leg.metadata_={**(leg.metadata_ or {}),'ota_owner_revenue':op['net']}
                meta['ota_owner_revenue']=op['net']
                meta['price_locked']=True
                meta=lock_fields(meta,['ota_owner_revenue'])
                # The explicit per-room net includes any subsidy; don't keep
                # an old platform subsidy available to another fallback reader.
                meta['ota_subsidy']='0'
            if op['kind']=='transfer_allocation':meta['confirmed_room_transfer']={**confirmation,'orders':op['group'],'nights':op['nights']}
            order.metadata_=meta
            from app.services.order_pricing import sync_ota_commission_rate
            if op.get('net') is not None:
                sync_ota_commission_rate(order)
        await log_action_tx(db,who,'monthly_close.business_correction','order',order.order_id,before_data=before,
            after_data={'operation':op,'metadata':order.metadata_,'channel':order.channel.value,'platform_order_id':order.platform_order_id},notes=proposal.proposal_id)
        await invalidate_monthly_close_subject(db,cycle_id=cycle.cycle_id,subject_type='order',subject_id=order.order_id,
            event_id=f'{proposal.proposal_id}:{order.order_id}',cause='管理员确认业务修正',actor_id=who,evidence_hash=digest(op))
    for item in proposal.payload.get('feedback_evidence',[]):
        source=sources[item['source_id']]
        source.decisions={**source.decisions,item['fact_key']:{'status':'applied','proposal_id':proposal.proposal_id,'confirmed_by':who,'confirmed_at':now}}
        source.version+=1
    await db.flush()
    proposal.status='completed'
    proposal.result=dict(kind='business_correction',count=len(proposal.payload['operations'])+len(proposal.payload.get('source_changes',[])),amount='0.00',details=proposal.payload['details'])
    await log_action_tx(db,who,'monthly_close.business_correction_completed','financial_case_proposal',proposal.proposal_id,after_data=proposal.result)
