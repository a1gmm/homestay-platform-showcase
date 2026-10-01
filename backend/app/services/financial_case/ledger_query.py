"""Read-only scope and trace for the exact expenses used in a chat total."""
import re
import json
from decimal import Decimal
from sqlalchemy import select
from app.models.room import Room
from app.models.order import Order
from app.models.expense import ExpensePayer, EXPENSE_CATEGORY_LABELS
from app.services.reconciliation_policy import confirmed_cost_bearer


def is_scope_followup(text):
    return bool(re.search(r'这些|那些|刚才|那.*呢|排除|不算|去掉|只看|为什么少|为什么多|怎么少|怎么多', text))


async def summarize(db, rows, text, previous_texts, months, category_label, prior_scope=None):
    prior_scope = prior_scope or {}
    query=text
    if re.search(r'这些|那些|刚才|那.*呢|排除|不算|去掉|只看',text):
        for prior in reversed(previous_texts):
            if prior.startswith('用户：') and re.search(r'保洁|洗涤|费用|支出',prior):
                query=prior+' '+text
                break
    rooms={r.room_id:r for r in await db.scalars(select(Room))}
    orders={o.order_id:o for o in await db.scalars(select(Order).where(Order.order_id.in_({r.order_id for r in rows if r.order_id})))}
    floor_matches=re.findall(r'(?<!\d)(\d{1,2})\s*层',text) or prior_scope.get('floors', []) or re.findall(r'(?<!\d)(\d{1,2})\s*层',query)
    floors={int(f) for f in floor_matches} or set(prior_scope.get('floors', []))
    room_refs=set(re.findall(r'(?<!\d)(\d{3,5})(?:房|号房)',text)) or set(prior_scope.get('rooms', []))
    if re.search(r'全部楼层|所有楼层|两层合计', text):floors=set()
    exclude_reception=bool(prior_scope.get('exclude_reception') or re.search(r'(?:排除|不算|去掉|不含|除去)\s*(?:公司)?接待',query))
    if re.search(r'加回.*接待|包含.*接待',text):exclude_reception=False
    payer=ExpensePayer(prior_scope['payer']) if prior_scope.get('payer') else None
    if re.search(r'只(?:看|算).*业主承担',query):payer=ExpensePayer.owner
    if re.search(r'只(?:看|算).*公司承担',query):payer=ExpensePayer.company
    def floor(room):
        if room is None:return None
        if room.floor is not None:return room.floor
        name=str(room.room_name or '')
        return int(name[:-2]) if re.fullmatch(r'\d{3,5}',name) else None
    selected=[e for e in rows if (not floors or floor(rooms.get(e.room_id)) in floors)
        and (not room_refs or str(getattr(rooms.get(e.room_id),'room_name','')) in room_refs)
        and (payer is None or e.payer==payer)
        and not (exclude_reception and e.order_id in orders and confirmed_cost_bearer(orders[e.order_id].metadata_)==ExpensePayer.company)]
    cleaning_kind=prior_scope.get('cleaning_kind')
    if '续住' in text and not re.search(r'正常|退房',text):cleaning_kind='renewal'
    elif re.search(r'正常打扫|退房打扫',text) and '续住' not in text:cleaning_kind='checkout'
    elif '续住' in text and re.search(r'正常|退房',text):cleaning_kind=None
    if cleaning_kind=='renewal':
        selected=[e for e in selected if (e.description or '').startswith('续住打扫')]
    elif cleaning_kind=='checkout':
        selected=[e for e in selected if (e.description or '').startswith('退房打扫')]
    amount=sum((e.amount for e in selected),Decimal(0))
    company=sum((e.amount for e in selected if e.payer==ExpensePayer.company),Decimal(0))
    owner=amount-company
    scope='、'.join(months)+((' · '+'、'.join(str(f)+'层' for f in sorted(floors))) if floors else '')
    if room_refs:scope+=' · '+'、'.join(sorted(room_refs))+'房'
    if exclude_reception:scope+=' · 排除已确认公司接待'
    message=f'{scope}，系统已记{category_label}共 {len(selected)} 笔，合计 {amount:,.2f} 元。其中台账标记业主承担 {owner:,.2f} 元、公司承担 {company:,.2f} 元。供应商实际付款另行核对，未记账不等于没有费用。'
    details=[]
    from app.models.financial_case import FinancialCaseSource
    from app.models.monthly_close import MonthlyCloseDocument, MonthlyCloseCycle
    from app.models.settlement import OwnerSettlement, OwnerSettlementItem
    source_ids={e.notes.split(':')[1] for e in selected if (e.notes or '').startswith('financial-case:') and len(e.notes.split(':'))==3}
    sources={s.source_id:s for s in await db.scalars(select(FinancialCaseSource).where(FinancialCaseSource.source_id.in_(source_ids)))}
    documents={d.document_id:d for d in await db.scalars(select(MonthlyCloseDocument).join(MonthlyCloseCycle,MonthlyCloseCycle.cycle_id==MonthlyCloseDocument.cycle_id).where(MonthlyCloseCycle.billing_month.in_(months)))}
    settlement_rows=(await db.execute(select(OwnerSettlementItem,OwnerSettlement).join(OwnerSettlement,OwnerSettlement.settlement_id==OwnerSettlementItem.settlement_id).where(OwnerSettlement.billing_month.in_(months)))).all()
    included={}
    statuses={'pending':'待确认结算','confirmed':'已确认结算','paid':'已登记打款结算','disputed':'争议结算'}
    for item,settlement in settlement_rows:
        for charge in item.cost_share_breakdown or []:
            if charge.get('expense_id'):
                included.setdefault(charge['expense_id'],[]).append(f"{settlement.billing_month} {statuses[settlement.status.value]}扣给业主 {Decimal(charge['owner_amount']):.2f} 元")
    for e in selected:
        o=orders.get(e.order_id);room=rooms.get(e.room_id)
        name=f' · {o.guest_name}' if o and o.guest_name else ''
        lineage='来源资料已关联' if (e.notes or '').startswith('financial-case:') else ('系统按服务规则生成' if e.is_service_fee else '手工登记')
        if (e.notes or '').startswith('financial-case:') and len(e.notes.split(':'))==3:
            _,source_id,key=e.notes.split(':')
            source=sources.get(source_id)
            fact=next((f for f in source.parsed.get('facts',[]) if f.get('key')==key),None) if source else None
            if fact:lineage=f"来源：{source.filename} · {fact.get('sheet','原表')} 第 {fact.get('row1based',fact.get('row'))} 行"
        elif (e.notes or '').startswith('{'):
            try:work=json.loads(e.notes)
            except (ValueError,TypeError):work={}
            if isinstance(work,dict) and work.get('source')=='confirmed_cleaning_work':
                document=documents.get(work.get('document_id'))
                lineage=f"来源：{document.filename if document else '保洁原件'} · {work.get('source_sheet','原表')} 第 {work.get('source_row','待核实')} 行；{work.get('quantity','待核实')} 次 × {work.get('unit_price','待核实')} 元，应计 {work.get('expected_amount','待核实')} 元，本笔补入 {e.amount:.2f} 元"
        paid=('公司' if e.paid_by==ExpensePayer.company else '业主') if e.paid_by else '未登记'
        settlement_note='；'.join(included.get(e.expense_id,[])) or '当前结算明细未列入这笔业主扣费'
        details.append(dict(label=f'{e.expense_date} · {room.room_name if room else "公共费用"}{name}',
            value=f'{EXPENSE_CATEGORY_LABELS[e.category]} · {e.description or "无备注"} · {e.amount:.2f} 元 · {"业主" if e.payer==ExpensePayer.owner else "公司"}承担；{lineage}。付款人：{paid}；付款日：{e.payment_date or "未登记"}；{settlement_note}。'))
    return message,details,dict(months=months,floors=sorted(floors),rooms=sorted(room_refs),
        exclude_reception=exclude_reception,payer=payer.value if payer else None,cleaning_kind=cleaning_kind)
