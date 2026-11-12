"""Record a human verification of an existing fee without writing financial state.

The enclosing assistant persists the original statement and this bounded reply.
This is neither a fee amendment nor evidence that a supplier was paid or a task
completed at a particular time.
"""
import re
from decimal import Decimal

from sqlalchemy import select

from app.models.expense import Expense, ExpenseCategory, ExpensePayer
from app.models.room import Room
from .ledger_query import room_refs, room_number, stated_dates


def wants(text):
    return bool(re.search(r'实际.*打扫|确实.*打扫|已经打扫|已打扫|表.*漏填|表.*漏记', text)
                and re.search(r'照常收|正常收|保留|不.*改.*金额|收费正确|仅记录|只记录|确认', text))


def snapshot(row):
    return dict(expense_id=row.expense_id, amount=f'{row.amount:.2f}',
                room_id=row.room_id, order_id=row.order_id, owner_id=row.owner_id,
                expense_date=str(row.expense_date), payer=row.payer.value,
                category=row.category.value, is_deleted=row.is_deleted)


async def respond(db, cycle, text, previous):
    def clarify(message):
        return dict(state='needs_information', message=message, export_ready=False)

    # Contradictions, questions and additional changes must never be reported
    # as an accepted verification, even if they contain an affirmative clause.
    intent = re.sub(r'不新增费用|不改金额[、，或]*(?:承担方[、，或]*)?(?:或?账单)?|不补造打扫完成时间', '', text)
    if (re.search(r'(?:没(?:有)?|未|不曾)(?:实际|已经|确实|已)?打扫|不是.*打扫|不确定|不清楚|待核实|可能|应该|估计|是否|[？?]|改成|改为|更正|删除|取消|退费|退款|再加|另外|补记|新增', intent)
            or not re.search(r'实际(?:已经|已|确实)?(?:完成)?打扫(?:了)?|确实(?:已经|已)?打扫|已经打扫|已打扫', text)):
        return clarify('这段说明还包含疑问或变更要求，请明确实际是否打扫，以及原收费是否保留。当前未新增或修改费用。')
    months = set(re.findall(r'(?<!\d)(20\d{2}-\d{2})(?!\d)', text))
    months.update(f'{year or cycle.billing_month[:4]}-{int(month):02d}'
                  for year, month in re.findall(r'(?:(20\d{2})年)?(1[0-2]|0?[1-9])月', text))
    if months - {cycle.billing_month}:
        return clarify('这条确认提到了其他月份，请切换到对应月份后核对；本月账目未修改。')
    ids = set(re.findall(r'EXP-[A-Za-z0-9-]+', text))
    order_ids = set(re.findall(r'ORD-[A-Za-z0-9-]+', text))
    refs, dates = room_refs(text), stated_dates(text, cycle.billing_month)
    prior = previous.facts.get('ledger_scope') or {} if previous and previous.tool == 'financial_case' else {}
    explicit = bool(ids or order_ids or refs or dates)
    if not explicit:
        ids = set(prior.get('expense_ids') or [])
    if len(ids) > 1 or len(order_ids) > 1 or len(refs) > 1:
        return clarify('这段确认对应多笔记录，请逐笔说明房间、退房日期或费用编号；当前未修改账目。')
    if not ids and not order_ids and not (refs and dates):
        return clarify('请补充是哪间房、哪天退房的保洁，或先查询具体费用再确认。我会保留已有记录，不会重复记账。')
    query = select(Expense).where(Expense.is_deleted.is_(False), Expense.category == ExpenseCategory.cleaning)
    if ids:
        query = query.where(Expense.expense_id.in_(ids))
    if order_ids:
        query = query.where(Expense.order_id.in_(order_ids))
    rooms = {room.room_id: room for room in await db.scalars(select(Room))}
    rows = [row for row in await db.scalars(query)
            if row.expense_date.strftime('%Y-%m') == cycle.billing_month
            and (not refs or room_number(getattr(rooms.get(row.room_id), 'room_name', '')) in refs)
            and (not dates or str(row.expense_date) in dates)]
    if len(rows) != 1:
        return clarify('没有找到唯一对应的本月有效保洁费。请核对房间、退房日期或费用编号；未找到记录不代表没有打扫，本次未新增或修改费用。')
    row = rows[0]
    if not explicit and prior.get('confirmed_expense') and snapshot(row) != prior['confirmed_expense']:
        return clarify('这笔费用在上次查看后发生变化，请重新查询并核对金额和承担方，再确认；本次未修改账目。')
    amounts = {Decimal(value.replace(',', '')) for value in re.findall(r'(?<![\d.])([+-]?\d[\d,]*(?:\.\d{1,2})?)\s*(?:元|块)', text)}
    if amounts and amounts != {row.amount}:
        return clarify(f'你确认的金额与当前台账 {row.amount:.2f} 元不一致，请先核实差额；本次保留现有费用，未修改账单。')
    stated_payers = {party for label, party in [('业主', ExpensePayer.owner), ('房东', ExpensePayer.owner), ('公司', ExpensePayer.company)]
                    if re.search(label + r'(?:那边)?(?:来)?承担', text)}
    if stated_payers and stated_payers != {row.payer}:
        return clarify('你说明的承担方与当前台账不一致，请先核实承担方；本次未修改费用或业主账单。')
    room = rooms.get(row.room_id)
    label = room.room_name if room else '未关联房间'
    payer = '业主' if row.payer == ExpensePayer.owner else '公司'
    source_note = '、原表漏填' if re.search(r'表.{0,8}漏(?:填|记)', text) else ''
    return dict(state='result', export_ready=False,
                message=f'已记录你的确认：{label} {row.expense_date} 的保洁实际已完成{source_note}。已核对原有 {row.amount:.2f} 元保洁费，继续由{payer}承担；未新增费用，金额和账单均未修改。',
                details=[dict(label='核对的原有费用', value=f'{row.expense_id} · {row.expense_date} · {row.amount:.2f} 元 · {payer}承担'),
                         dict(label='确认依据', value='管理员本次说明已保存在对话中。原始表格保留，未补造保洁任务完成时间，也未登记供应商付款。')],
                ledger_scope=dict(months=[cycle.billing_month], categories=['cleaning'],
                                  rooms=[room_number(label)] if room else [],
                                  floors=[], exclude_reception=False, payer=row.payer.value,
                                  cleaning_kind=None, confirmation_recorded=True,
                                  expense_ids=[row.expense_id], confirmed_expense=snapshot(row)))
