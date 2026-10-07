"""Case orchestration: source facts -> explicit decisions -> bound plan -> audited ledger.

All mutations participate in the caller's transaction. No model output supplies
amounts, record IDs or SQL. Unresolved evidence never silently becomes a zero.
"""
from __future__ import annotations
from collections import Counter, defaultdict
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json
import re
import calendar
from uuid import uuid4

from sqlalchemy import select, or_
from app.models.monthly_close import MonthlyCloseDocument, MonthlyCloseCycle
from app.models.financial_case import FinancialCaseSource, FinancialCaseProposal
from app.models.expense import Expense, ExpenseCategory, ExpensePayer, EXPENSE_CATEGORY_LABELS
from app.models.room import Room
from app.models.order import Order
from app.models.user import User
from app.models.settlement import OwnerSettlement, SettlementStatus
from app.services.audit import log_action_tx
from app.services.monthly_close.workflow import require_cycle_writable, MonthlyCloseConflict
from app.services.monthly_close.financial_lock import acquire_month_financial_lock
from .bank_parser import parse_bank_workbook
from .source_parsers import parse_financial_workbook
from .cross_month import (normalize_months, cycle_view, period_snapshot,
                          lock_writable_months, ensure_month_cycles, operation_months)

VERSION = 'financial-case-v1'
CATEGORIES = {'water':'水费', 'electricity':'电费', 'property':'物业费', 'maintenance':'维保费',
              'operating_income':'经营收入','capital':'投资款','loan':'借款','capital_return':'退投资款',
              'loan_repayment':'还借款','internal_transfer':'内部转账','owner_distribution':'业主分成',
              'payroll':'工资','social_insurance':'五险一金','tax':'税费','bank_fee':'手续费',
              'utility':'水电费','cleaning_supplier':'保洁供应商费用','laundry_supplier':'洗涤供应商费用',
              'supplies':'采购','rent':'租金','operating_expense':'其他运营费用',
              'cleaning_supplier_cost':'保洁供应商成本','laundry_supplier_cost':'洗涤供应商成本',
              'revenue':'营业收入','ota':'平台结算','unknown':'未分类'}
EXPENSE_CATEGORIES = {'water':ExpenseCategory.water,'electricity':ExpenseCategory.electricity,
                      'property':ExpenseCategory.property_fee,'maintenance':ExpenseCategory.maintenance}


def digest(value):
    return sha256(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'), default=str).encode()).hexdigest()


def actor_id(actor):
    return actor.get('user_id') if isinstance(actor, dict) else actor.user_id


async def require_admin(db, actor, *, lock=False):
    query = select(User).where(User.user_id == actor_id(actor)).execution_options(populate_existing=True)
    if lock:
        query = query.with_for_update()
    user = await db.scalar(query)
    if not user or not user.is_active or getattr(user.role, 'value', user.role) != 'admin':
        raise PermissionError('此功能仅管理员可用')
    return user


async def has_sources(db, cycle_id):
    return await db.scalar(select(FinancialCaseSource.source_id).where(FinancialCaseSource.cycle_id == cycle_id).limit(1)) is not None


async def load_sources(db, cycle_id):
    return list((await db.scalars(select(FinancialCaseSource).outerjoin(
        MonthlyCloseDocument, MonthlyCloseDocument.document_id == FinancialCaseSource.document_id).where(
        FinancialCaseSource.cycle_id == cycle_id,
        or_(FinancialCaseSource.document_id.is_(None), MonthlyCloseDocument.is_active.is_(True)))
                                 .order_by(FinancialCaseSource.created_at, FinancialCaseSource.source_id)
                                 .execution_options(populate_existing=True))).all())


async def validate_attachments(db, cycle_id, ids, *, include_inactive=False):
    if include_inactive:
        known = set((await db.scalars(select(FinancialCaseSource.source_id).where(FinancialCaseSource.cycle_id==cycle_id))).all())
    else:
        known = {s.source_id for s in await load_sources(db, cycle_id)}
    if any(item not in known for item in ids):
        raise ValueError('附件不存在或不属于当前月份')


def source_payload(source):
    return dict(source_id=source.source_id, filename=source.filename, kind=source.kind, parsed=source.parsed,decisions=source.decisions)


def receipt(source):
    return dict(source_id=source.source_id, item_id=source.source_id, filename=source.filename,
                kind=source.kind, fact_count=len(source.parsed.get('facts', [])),
                issues_count=len(source.parsed.get('issues', [])), issues=source.parsed.get('issues', [])[:20])


async def receive_file(db, cycle, actor, data, filename, mime_type):
    await require_admin(db, actor)
    if not data or len(data) > 10 * 1024 * 1024:
        raise ValueError('文件不能为空，且不能超过 10 MB')
    filename = filename.replace('\\', '/').split('/')[-1][:255]
    if not re.search(r'\.(xlsx?|docx|png|jpe?g)$', filename, re.I):
        raise ValueError('支持 Excel、Word 核实清单、PNG 和 JPEG 原件')
    fingerprint = sha256(data).hexdigest()
    cycle = await require_cycle_writable(db, cycle)
    existing = await db.scalar(select(FinancialCaseSource).where(
        FinancialCaseSource.cycle_id == cycle.cycle_id, FinancialCaseSource.sha256 == fingerprint))
    if existing:
        if existing.document_id:
            document = await db.get(MonthlyCloseDocument, existing.document_id, populate_existing=True)
            if not document or not document.is_active:
                raise ValueError('这份原件已撤回，请在资料管理中恢复后重新核对；不会重复导入或使用已撤回资料。')
        return receipt(existing)
    if len(await load_sources(db,cycle.cycle_id))>=100:
        raise ValueError('一个对账工作区最多保存 100 份来源，请按月份分别整理')
    # CPU parsing runs off the event loop; archive remains atomic with its facts.
    import asyncio
    if filename.lower().endswith('.docx'):
        from .feedback import parse_feedback
        parsed = await asyncio.to_thread(parse_feedback,data,filename)
    elif re.search(r'\.(png|jpe?g)$', filename, re.I):
        from .images import parse_image
        parsed = await asyncio.to_thread(parse_image, data, filename)
    else:
        def parse():
            from .feedback import parse_feedback
            return parse_feedback(data, filename) or parse_financial_workbook(data, filename) or parse_bank_workbook(data, filename)
        parsed = await asyncio.to_thread(parse)
        if parsed is None:
            parsed = dict(kind='unknown',facts=[],summaries=[],templates=[],issues=[dict(
                code='unsupported_template', message='原件已保存，但尚未识别这份表的结构。请说明表里记录的业务，不能直接入账。')])
    await require_admin(db, actor)
    source = FinancialCaseSource(source_id='FCS-'+uuid4().hex[:20].upper(),cycle_id=cycle.cycle_id,
        filename=filename,sha256=fingerprint,kind=parsed['kind'],content=data,parsed=parsed,
        decisions={},version=1,created_by=actor_id(actor))
    # Keep existing date-level cleaning matching and fee controls available.
    if parsed['kind'] in ('cleaning','utility','ota'):
        from app.services.monthly_close.documents import store_document
        document = await store_document(db, cycle, source_type={'cleaning':'cleaning_statement','utility':'utility_expense','ota':'ota_statement'}[parsed['kind']],filename=filename,
            mime_type=mime_type or 'application/octet-stream',data=data,user_id=actor_id(actor),commit=False)
        source.document_id = document.document_id
    db.add(source)
    await db.flush()
    await log_action_tx(db, actor_id(actor), 'financial_case.source_received','financial_case_source',source.source_id,
                        after_data={'sha256':fingerprint,'kind':source.kind,'facts':len(parsed['facts'])})
    return receipt(source)


def _issue(code, message, source=None, fact=None):
    return dict(code=code,message=message,source_id=source.source_id if source else None,
                fact_key=fact.get('key') if fact else None)


async def ledger(db, cycle):
    # Bind all rows in this month including tombstones; undo/recreate cannot replay a stale plan.
    start = date.fromisoformat(cycle.billing_month+'-01')
    end = date(start.year+int(start.month==12),start.month%12+1,1)
    rows = list((await db.scalars(select(Expense).where(Expense.expense_date >= start,Expense.expense_date < end)
                                .order_by(Expense.expense_id).execution_options(populate_existing=True))).all())
    return rows


def ledger_snapshot(rows):
    return [dict(id=e.expense_id,date=str(e.expense_date),amount=str(e.amount),category=e.category.value,
                 room=e.room_id,owner=e.owner_id,payer=e.payer.value,deleted=e.is_deleted,
                 service=e.is_service_fee,description=e.description,
                 paid_by=getattr(getattr(e,'paid_by',None),'value',None),payment_date=str(getattr(e,'payment_date',None))) for e in rows]


async def report_snapshot(db, cycle, sources, months, rows=None):
    """Rebuild the complete report only from its exact source/ledger evidence."""
    from .reporting import build_report
    if rows is None:
        rows = [e for month in months for e in await ledger(db, cycle_view(cycle, month))]
    rows = sorted(rows, key=lambda e: e.expense_id)
    grouped = defaultdict(Decimal)
    for e in rows:
        if not e.is_deleted:
            grouped[e.category.value] += e.amount
    payloads = [source_payload(s) for s in sources]
    report = build_report(payloads, {k: v for s in sources for k, v in s.decisions.items()}, months,
        ledger_summary={'total': f'{sum(grouped.values(), Decimal(0)):.2f}',
                        'by_category': {k: f'{v:.2f}' for k, v in grouped.items()}})
    evidence_hash = digest(dict(months=months, sources=payloads,
        originals=[dict(id=s.source_id, sha256=s.sha256, version=s.version) for s in sources],
        ledger=ledger_snapshot(rows)))
    return report, dict(evidence_hash=evidence_hash, issue_count=len(report['issues']))


def posting_date(fact, decision):
    explicit = decision.get('expense_date')
    if explicit:
        return explicit
    month = decision.get('business_month',fact.get('business_month'))
    if decision.get('status')=='confirmed' and decision.get('date_basis')=='month_end_accrual' and month:
        year,number=map(int,month.split('-'))
        return date(year,number,calendar.monthrange(year,number)[1]).isoformat()
    return fact.get('date')


async def match_sources(db, cycle, sources):
    rooms = list((await db.scalars(select(Room).where(Room.is_deleted.is_(False)).order_by(Room.room_id))).all())
    from app.services.monthly_close.operating_expenses import build_room_aliases
    aliases = build_room_aliases([(r.room_id, r.room_name) for r in rooms])
    room_map = {r.room_id:r for r in rooms}
    rows = await ledger(db, cycle)
    # A source already posted in another month (or later deleted) must remain
    # bound to its original row. Reclassification cannot silently recreate it.
    if sources:
        linked = list((await db.scalars(select(Expense).where(or_(*[
            Expense.notes.startswith(f'financial-case:{source.source_id}:') for source in sources
        ])).order_by(Expense.expense_id).execution_options(populate_existing=True))).all())
        rows = sorted({e.expense_id:e for e in rows+linked}.values(),key=lambda e:e.expense_id)
    operations, issues, matches = [], [], []
    seen = set()
    for source in sources:
        for raw in source.parsed.get('issues', []):
            if raw['code'] == 'expense_payer_unconfirmed':
                continue  # Per-row actionable questions below replace this generic source warning.
            issues.append(_issue(raw['code'], raw.get('message') or str(raw.get('details') or raw['code']), source))
        for fact in source.parsed.get('facts', []):
            if fact.get('kind') not in ('source_expense','image_expense_candidate'):
                continue
            decision = source.decisions.get(fact['key'], {})
            if decision.get('include') is False and decision.get('status') == 'confirmed':
                continue
            declared_month=decision.get('business_month',fact.get('business_month'))
            if declared_month and declared_month != cycle.billing_month:
                issues.append(_issue('expense_period_unknown',f'这条费用属于 {declared_month}，请明确选择该业务月份生成方案。',source,fact))
                continue
            image_candidate = fact.get('kind')=='image_expense_candidate'
            if image_candidate and (decision.get('status')!='confirmed' or decision.get('candidate_confirmed') is not True):
                issues.append(_issue('image_amount_unconfirmed',f"{source.filename}：已识别出可能的费用 {fact.get('amount','')} 元，请先核实图片金额、用途和期间。",source,fact));continue
            if image_candidate and decision.get('paid_by') not in ('company','owner'):
                issues.append(_issue('image_payment_unconfirmed','请说明图片中的这笔费用实际由公司还是业主支付，再生成记账方案。',source,fact));continue
            if image_candidate:
                from .image_facts import effective_image_fact
                fact=effective_image_fact(fact,decision)
                fields=fact.get('fields') or {}
                if fields.get('allocation_required') and decision.get('payer')=='owner':
                    issues.append(_issue('image_owner_allocation_required','图片合计涉及多个房间，请按已核实的分房明细及优惠分摊规则记给各业主，不能把合计扣给一个房间。',source,fact));continue
                if fields.get('discount_allocation_required') and fields.get('applied_discount_allocation')!='proportional':
                    issues.append(_issue('image_discount_unconfirmed','图片明细金额未分摊总优惠，请明确是否按各房原金额比例分摊后再入账。',source,fact));continue
            category_key = decision.get('category', fact.get('category'))
            category = EXPENSE_CATEGORIES.get(category_key)
            if not category:
                try: category=ExpenseCategory({'cleaning_supplier':'cleaning_supplier_cost','laundry_supplier':'laundry_supplier_cost'}.get(category_key,category_key))
                except ValueError: category=None
            business_month = decision.get('business_month', fact.get('business_month'))
            reference = decision.get('room_ref',fact.get('room_ref'))
            room_id = aliases.get(reference) or (reference if reference in room_map else None)
            composite = re.fullmatch(r'\d+-\d+-\d+-(\d{4})', reference or '')
            if not room_id and composite:
                room_id = aliases.get(composite.group(1))
            public_company_cost = image_candidate and not reference and decision.get('payer')=='company'
            if not room_id and not public_company_cost:
                issues.append(_issue('room_unmapped',f"原表第 {fact.get('row1based')} 行房号 {reference or '空缺'} 无法唯一对应系统房间。",source,fact));continue
            posting_day = posting_date(fact,decision)
            if not posting_day or not category or business_month != cycle.billing_month or posting_day[:7] != cycle.billing_month:
                issues.append(_issue('expense_period_unknown','这条支出的类别或日期不属于当前工作区，请明确业务月份后在对应月份处理。',source,fact));continue
            try:
                amount = Decimal(fact['amount'])
                if not amount.is_finite(): raise ValueError('nonfinite')
            except (ValueError, TypeError, ArithmeticError):
                issues.append(_issue('invalid_expense_amount','金额无法可靠识别，请核对原件。',source,fact));continue
            if amount <= 0:
                issues.append(_issue('nonpositive_expense','退款或零金额需单独核实，不作为新增支出。',source,fact));continue
            if amount >= Decimal('100000000') or amount != amount.quantize(Decimal('.01')):
                issues.append(_issue('invalid_expense_amount','费用金额超出可保存范围或精度，请核对原件，不能自动四舍五入记账。',source,fact));continue
            prior = [e for e in rows if e.notes==f"financial-case:{source.source_id}:{fact['key']}"]
            if any(e.is_deleted for e in prior):
                issues.append(_issue('source_expense_removed','这条来源对应的系统费用已作废，请先核实撤销原因，不能通过重复上传或修改月份重新入账。',source,fact));continue
            if prior and any(str(e.expense_date)!=posting_day or e.category!=category or e.amount!=amount or e.room_id!=room_id for e in prior):
                issues.append(_issue('source_expense_changed','这条来源已经记入系统，但月份、房间、分类或金额与本次说明不同，请先更正已有费用，不能重复新增。',source,fact));continue
            exact = [e for e in rows if not e.is_deleted and e.room_id == room_id
                     and str(e.expense_date) == posting_day and e.category == category and e.amount == amount]
            signature = (room_id, posting_day,category.value,fact['amount'])
            if signature in seen:
                issues.append(_issue('duplicate_source_expense','其他来源已有同房、同日、同类、同金额的支出，需要确认是否同一笔。',source,fact));continue
            seen.add(signature)
            if exact:
                if len(exact) > 1:
                    issues.append(_issue('duplicate_ledger_expense','系统存在多条同房、同日、同类、同金额支出，请先核实重复记录。',source,fact));continue
                if decision.get('status') == 'confirmed' and decision.get('payer') in ('company','owner'):
                    expected_owner = room_map[room_id].owner_id if decision['payer']=='owner' and room_id else None
                    if exact[0].payer.value != decision['payer'] or exact[0].owner_id != expected_owner:
                        issues.append(_issue('ledger_payer_conflict','已有支出的承担方或业主与本次确认不一致，需先核实，不能重复新增或视为已对齐。',source,fact));continue
                    if decision.get('paid_by') in ('company','owner') and getattr(exact[0].paid_by,'value',None)!=decision['paid_by']:
                        issues.append(_issue('ledger_payment_conflict','已有费用的实际付款人与本次说明不同，请先更正付款依据，不能把它视为已核对。',source,fact));continue
                matches.append(dict(source_id=source.source_id,fact_key=fact['key'],expense_ids=[e.expense_id for e in exact]))
                continue
            if decision.get('status') != 'confirmed' or decision.get('payer') not in ('company','owner'):
                issues.append(_issue('payer_unconfirmed',f"{room_id or '公共费用'} {fact.get('date') or business_month or ''} {CATEGORIES.get(category_key)} {fact['amount']} 元：原表“已收”只代表供应方收款，请确认公司还是业主承担。",source,fact));continue
            room = room_map.get(room_id)
            if decision['payer'] == 'owner' and (not room or not room.owner_id):
                issues.append(_issue('owner_missing',f'{room_id} 尚未绑定业主，不能将费用记给业主。',source,fact));continue
            operations.append(dict(source_id=source.source_id,fact_key=fact['key'],room_id=room_id,
                expense_date=posting_day,category=category.value,amount=fact['amount'],payer=decision['payer'],
                owner_id=room.owner_id if decision['payer']=='owner' else None,
                expense_id='FC-'+digest({'fact':fact['key'],'cycle':cycle.cycle_id})[:17].upper(),
                description=f"{CATEGORIES.get(category_key, '资料支出')} · 原表第 {fact.get('row1based')} 行",
                paid_by=decision.get('paid_by') if decision.get('paid_by') in ('company','owner') else None,
                payment_date=fact.get('date'),business_month=business_month,date_basis=decision.get('date_basis','source_date')))
    from .posting import build_bank_operations
    from .deduplication import deduplicate_source_operations
    reconciled=deduplicate_source_operations(sources,rows,operations,existing_matches=matches)
    operations=reconciled['operations'];issues.extend(reconciled['issues']);matches.extend(reconciled['matches'])
    bank_result=await build_bank_operations(db,cycle,sources,rows,rooms,operations)
    operations.extend(bank_result['operations']);issues.extend(bank_result['issues']);matches.extend(bank_result['matches'])
    from .image_facts import compare_image_expense_sources
    for conflict in compare_image_expense_sources([source_payload(s) for s in sources]):
        names={s.source_id:s.filename for s in sources}
        issues.append(dict(code=conflict['code'],source_id=conflict.get('source_id'),fact_key=None,
            message=f"{CATEGORIES.get(conflict.get('category'),'同类费用')}：图片 {conflict.get('image_total')} 元，{names.get(conflict.get('other_source_id'),'费用表')} {conflict.get('workbook_total')} 元，相差 {conflict.get('difference')} 元。{conflict['message']}"))
    # A platform ID is only a candidate within its actual platform, not a room ID.
    ota = [(s,f) for s in sources for f in s.parsed.get('facts', []) if f.get('platform_order_id')]
    ids = {f['platform_order_id'] for _,f in ota}
    orders = list((await db.scalars(select(Order).where(Order.platform_order_id.in_(ids),Order.is_deleted.is_(False)))).all()) if ids else []
    for source, fact in ota:
        candidates = [o for o in orders if o.platform_order_id == fact['platform_order_id']
                      and getattr(o.channel,'value',o.channel) == fact.get('channel')]
        if len(candidates) != 1:
            channel_name = {'ctrip':'携程','douyin':'抖音','meituan':'美团','airbnb':'爱彼迎'}.get(fact.get('channel'),'平台')
            issues.append(_issue('platform_order_unmatched',f"{channel_name}订单 {fact['platform_order_id']} 未唯一对应系统订单；需要入住日期和房间，不能仅凭券或房型创建订单。",source,fact))
        else:
            matches.append(dict(source_id=source.source_id,fact_key=fact['key'],order_id=candidates[0].order_id))
    from app.services.monthly_close.financial_case_bridge import normalize_financial_case_issues
    issues = normalize_financial_case_issues(sources, issues)
    snapshot = dict(sources=[dict(id=s.source_id,sha=s.sha256,version=s.version,decisions=s.decisions) for s in sources],
                    ledger=ledger_snapshot(rows),rooms=[dict(id=r.room_id,owner=r.owner_id) for r in rooms])
    return dict(operations=operations,issues=issues,matches=matches,snapshot_hash=digest(snapshot),ledger=rows)


async def match_case(db, cycle, sources, months=None):
    """One uploaded case, explicitly selected months, one bound proposal."""
    months=normalize_months(months,cycle.billing_month)
    from app.services.monthly_close.financial_case_bridge import reconcile_posted_other_month_facts
    results=[await match_sources(db,cycle_view(cycle,month),sources) for month in months]
    results=[await reconcile_posted_other_month_facts(db,cycle_view(cycle,month),sources,result)
             for month,result in zip(months,results)]
    def unique(values):
        return list({digest(value):value for value in values}.values())
    rows={e.expense_id:e for result in results for e in result['ledger']}
    scope=normalize_months(months+[cycle.billing_month])
    declared={f['key']:s.decisions.get(f['key'],{}).get('business_month',f.get('business_month'))
              for s in sources for f in s.parsed.get('facts',[])}
    issues=[issue for month,result in zip(months,results) for issue in result['issues']
            if not (issue['code']=='expense_period_unknown' and declared.get(issue.get('fact_key')) in months
                    and declared[issue['fact_key']]!=month)]
    return dict(operations=unique([op for result in results for op in result['operations']]),
        issues=unique(issues),
        matches=unique([item for result in results for item in result['matches']]),
        ledger=[e for e in rows.values() if e.expense_date.strftime('%Y-%m') in months],
        snapshot_hash=digest(dict(months=months,results=[r['snapshot_hash'] for r in results],
                                  periods=await period_snapshot(db,scope))))


def select_operations(operations, selection):
    return [op for op in operations if
        (not selection.get('source_ids') or op['source_id'] in selection['source_ids']) and
        (not selection.get('categories') or op['category'] in selection['categories']) and
        (not selection.get('fact_keys') or op['fact_key'] in selection['fact_keys']) and
        (not selection.get('months') or op['expense_date'][:7] in selection['months'])]


async def confirm_proposal(db, cycle, actor, context_run_id):
    from app.core.config import settings
    if not settings.MONTHLY_CLOSE_PROPOSAL_EXECUTION_ENABLED:
        raise ValueError('当前已暂停费用写入，方案尚未执行')
    await require_admin(db, actor)
    # Read scope first; sorted multi-month locks must precede every cycle lock.
    initial = await db.scalar(select(FinancialCaseProposal).where(
        FinancialCaseProposal.cycle_id==cycle.cycle_id,FinancialCaseProposal.run_id==context_run_id,
        FinancialCaseProposal.created_by==actor_id(actor)))
    if not initial:
        raise ValueError('请先查看具体记账方案，再确认这份方案')
    if initial.status=='completed':
        return initial,True
    months=normalize_months(initial.payload.get('selection',{}).get('months'),cycle.billing_month)
    owners=sorted(set(initial.payload.get('owner_ids',[])) | {op['owner_id'] for op in initial.payload.get('operations',[]) if op.get('owner_id')})
    await lock_writable_months(db,normalize_months(months+[cycle.billing_month]),actor_id(actor),owner_ids=owners)
    cycle = await require_cycle_writable(db, cycle)
    await require_admin(db, actor, lock=True)
    proposal = await db.scalar(select(FinancialCaseProposal).where(
        FinancialCaseProposal.cycle_id==cycle.cycle_id,FinancialCaseProposal.run_id==context_run_id,
        FinancialCaseProposal.created_by==actor_id(actor)).with_for_update().execution_options(populate_existing=True))
    if not proposal:
        raise ValueError('请先查看具体记账方案，再确认这份方案')
    if proposal.status == 'completed':
        return proposal, True
    if proposal.status != 'pending':
        raise ValueError('这份方案已取消或失效，请重新生成')
    if proposal.payload.get('type') == 'business_correction':
        from .operations import execute
        await execute(db,cycle,actor,proposal)
        return proposal,False
    sources = await load_sources(db,cycle.cycle_id)
    current = await match_case(db,cycle,sources,months)
    operations=select_operations(current['operations'],proposal.payload.get('selection',{}))
    if current['snapshot_hash'] != proposal.snapshot_hash or (proposal.payload.get('type') != 'interpretation' and operations != proposal.payload['operations']):
        raise ValueError('资料解释或系统记录已变化，请重新生成方案，旧方案没有执行')
    if proposal.payload.get('type') == 'interpretation':
        count=await apply_decisions(db,cycle,actor,proposal.payload['changes'])
        proposal.status='completed'
        proposal.result=dict(kind='interpretation',count=count,amount='0.00')
        await log_action_tx(db,actor_id(actor),'financial_case.interpretation_confirmed','financial_case_proposal',proposal.proposal_id,after_data=proposal.result)
        return proposal,False
    affected=operation_months(operations,months)
    if affected:
        await ensure_month_cycles(db,affected,actor_id(actor))
    for op in operations:
        db.add(Expense(expense_id=op['expense_id'],category=ExpenseCategory(op['category']),
            amount=Decimal(op['amount']),expense_date=date.fromisoformat(op['expense_date']),
            room_id=op['room_id'],owner_id=op['owner_id'],payer=ExpensePayer(op['payer']),
            description=op['description'],notes=f"financial-case:{op['source_id']}:{op['fact_key']}",
            created_by=actor_id(actor),is_service_fee=False,
            paid_by=ExpensePayer(op['paid_by']) if op.get('paid_by') in ('company','owner') else None,
            payment_date=date.fromisoformat(op['payment_date']) if op.get('payment_date') else None))
    await db.flush()
    proposal.status='completed'
    proposal.result=dict(count=len(operations),amount=format(sum((Decimal(o['amount']) for o in operations),Decimal(0)),'.2f'),months=affected)
    await log_action_tx(db,actor_id(actor),'financial_case.expenses_posted','financial_case_proposal',proposal.proposal_id,
                        after_data={'result':proposal.result,'operations':operations})
    return proposal, False


def _months(text, billing_month, sources):
    year = billing_month[:4]
    explicit = re.findall(r'(20\d{2})[-年](0?[1-9]|1[0-2])(?:月|\b)', text)
    months = [f'{y}-{int(m):02}' for y,m in explicit]
    if not months:
        months = [f'{year}-{int(m):02}' for m in re.findall(r'(?<!\d)(1[0-2]|0?[1-9])月',text)]
    if re.search(r'累计|开业|从开始|从开|全部月份',text):
        dates = [f.get('date') for s in sources for f in s.parsed.get('facts',[]) if f.get('date')]
        start = min(dates)[:7] if dates else billing_month
        stop = max(months or [billing_month])
        y,m=map(int,start.split('-')); months=[]
        while f'{y}-{m:02}' <= stop and len(months)<120:
            months.append(f'{y}-{m:02}'); y,m=(y+1,1) if m==12 else (y,m+1)
    return sorted(set(months or [billing_month]))


def validate_decisions(sources, changes):
    known = {f['key']:(s,f) for s in sources for f in s.parsed.get('facts',[])}
    allowed = {'category','business_month','payer','include','note','matching_fact_keys','expense_date','paid_by','room_ref','date_basis','candidate_confirmed','discount_allocation','independent_cost','owner_distribution_basis'}
    if not changes or len(changes)>2000:
        raise ValueError('请选择 1 至 2000 条具体来源记录')
    for change in changes:
        key=change['fact_key']; decision=change['decision']
        if not isinstance(key,str) or not isinstance(decision,dict):
            raise ValueError('来源记录与说明格式无效')
        for field in ('category','business_month','payer','paid_by','note','expense_date','room_ref','date_basis','discount_allocation'):
            if field in decision and not isinstance(decision[field],str):
                raise ValueError('说明字段必须为文本')
        if 'matching_fact_keys' in decision and (not isinstance(decision['matching_fact_keys'],list) or any(not isinstance(k,str) for k in decision['matching_fact_keys'])):
            raise ValueError('关联记录必须是来源标识列表')
        if key not in known or set(decision)-allowed:
            raise ValueError('来源记录或可修改字段无效')
        if decision.get('category') is not None and decision['category'] not in CATEGORIES:
            raise ValueError('费用分类无效')
        if decision.get('business_month') is not None and not re.fullmatch(r'20\d{2}-(0[1-9]|1[0-2])',decision['business_month']):
            raise ValueError('业务月份必须是 YYYY-MM')
        if decision.get('expense_date') is not None:
            try: date.fromisoformat(decision['expense_date'])
            except (ValueError,TypeError): raise ValueError('费用发生日期必须是有效的 YYYY-MM-DD')
        if decision.get('paid_by') is not None and decision['paid_by'] not in ('company','owner','unconfirmed'):
            raise ValueError('实际付款人必须是公司、业主或未确认')
        if decision.get('payer') is not None and decision['payer'] not in ('company','owner'):
            raise ValueError('承担方必须是公司或业主')
        if 'include' in decision and not isinstance(decision['include'],bool):
            raise ValueError('是否计入必须是明确的是或否')
        if 'candidate_confirmed' in decision and not isinstance(decision['candidate_confirmed'],bool):
            raise ValueError('图片金额必须明确核实')
        if 'independent_cost' in decision and not isinstance(decision['independent_cost'],bool):
            raise ValueError('是否独立费用必须明确核实')
        if decision.get('independent_cost') is True and decision.get('matching_fact_keys'):
            raise ValueError('独立费用不能同时关联为同一笔费用，请先澄清对应关系')
        if decision.get('date_basis') not in (None,'source_date','month_end_accrual'):
            raise ValueError('记账日期口径无效')
        if decision.get('discount_allocation') not in (None,'proportional'):
            raise ValueError('优惠分摊规则无效')
        if decision.get('owner_distribution_basis') not in (None,'net','gross'):
            raise ValueError('业主分成口径必须为扣费后净额或扣费前毛额')
        if len(decision.get('room_ref',''))>100:
            raise ValueError('房间说明过长')
        if len(decision.get('note',''))>1000:
            raise ValueError('说明不能超过 1000 字')
        if any(k not in known or k==key for k in decision.get('matching_fact_keys',[])):
            raise ValueError('关联的来源记录无效')
    effective={key:{**source.decisions.get(key,{})} for key,(source,_) in known.items()}
    for change in changes:
        effective[change['fact_key']].update(change['decision'],status='confirmed')
    for key,(_,fact) in known.items():
        decision=effective[key]
        if fact.get('kind')!='image_expense_candidate' or decision.get('candidate_confirmed') is not True or decision.get('include') is False:
            continue
        alternatives=fact.get('mutually_exclusive_with') or (fact.get('fields') or {}).get('mutually_exclusive_with',[])
        if any(effective.get(other,{}).get('candidate_confirmed') is True and effective[other].get('include') is not False for other in alternatives):
            raise ValueError('图片合计与明细不能同时入账，请选择一种并排除重复部分')
    return known


async def apply_decisions(db, cycle, actor, changes):
    """Explicit interpretations only; immutable date, direction, amount and raw text cannot be changed."""
    await require_admin(db,actor)
    await require_cycle_writable(db,cycle)
    sources = await load_sources(db,cycle.cycle_id)
    known = validate_decisions(sources, changes)
    for change in changes:
        s,f=known[change['fact_key']]
        before = s.decisions
        updated = dict(before)
        updated[f['key']]={**updated.get(f['key'],{}),**change['decision'],'status':'confirmed'}
        s.decisions=updated;s.version+=1
        await log_action_tx(db,actor_id(actor),'financial_case.fact_interpreted','financial_case_source',s.source_id,
                            before_data={f['key']:before.get(f['key'])},after_data={f['key']:updated[f['key']]})
    await db.flush()
    return len(changes)


async def _respond(db, cycle, actor, text, attachment_ids, context_run_id=None, *, run_id=None):
    from app.services.monthly_close.workflow import MonthlyCloseConflict
    await require_admin(db,actor)
    await validate_attachments(db,cycle.cycle_id,attachment_ids)
    sources = await load_sources(db,cycle.cycle_id)
    facts = dict(projection_version=VERSION,billing_month=cycle.billing_month,state='result',message='',
        sources=[{k:v for k,v in receipt(s).items() if k in ('source_id','filename','kind','fact_count')} for s in sources],
        metrics=[],issues=[],details=[],proposal_id=None,report_months=_months(text,cycle.billing_month,sources),export_ready=True)
    from .explanations import business_explanation
    if explanation := business_explanation(text):
        facts.update(explanation)
        return facts
    if re.fullmatch(r'[\s，。！!]*(确认执行|确认|通过|按这个方案执行)[\s，。！!]*',text):
        try:
            async with db.begin_nested():
                proposal,replayed=await confirm_proposal(db,cycle,actor,context_run_id)
        except (ValueError, MonthlyCloseConflict) as exc:
            facts.update(state='needs_information',message=str(exc));return facts
        if proposal.result.get('kind')=='business_correction':
            facts.update(state='completed',message=f"已处理 {proposal.result['count']} 项确认内容。具体修改见下方；来源解释不代表已经入账，结算需按更新后的账目重新复核。",proposal_id=proposal.proposal_id,proposal_kind='business_correction',details=proposal.result['details'])
            return facts
        if proposal.result.get('kind')=='interpretation':
            facts.update(state='completed',message=f"已保存 {proposal.result['count']} 条来源解释。原始金额未变，尚未新增系统支出；现在可以重新核对或生成记账方案。",proposal_id=proposal.proposal_id,proposal_kind='interpretation')
            return facts
        facts.update(state='completed',message=f"{'这份方案此前已执行' if replayed else '已记入系统支出'}：{proposal.result['count']} 条，共 {proposal.result['amount']} 元。原件和执行记录可追溯。",proposal_id=proposal.proposal_id,proposal_kind='posting')
        return facts
    pause_text = re.sub(r'[\s，,。！!；;]+', '', text)
    if re.fullmatch(r'(?:取消|先不执行|先别执行|不要执行|取消方案|(?:先)?(?:暂停|停止)(?:方案)?(?:不要执行|先不执行|别执行)?)', pause_text) and context_run_id:
        initial=await db.scalar(select(FinancialCaseProposal).where(FinancialCaseProposal.run_id==context_run_id,
            FinancialCaseProposal.created_by==actor_id(actor),FinancialCaseProposal.cycle_id==cycle.cycle_id))
        if initial:
            # Same month -> cycle -> proposal order as confirmation. Cancellation
            # remains available on a closed month and never creates a workspace.
            scope=normalize_months(initial.payload.get('selection',{}).get('months'),cycle.billing_month)
            scope=normalize_months(scope+[cycle.billing_month])
            for month in scope:
                await acquire_month_financial_lock(db,month)
            for month in scope:
                await db.scalar(select(MonthlyCloseCycle).where(MonthlyCloseCycle.billing_month==month)
                                .with_for_update().execution_options(populate_existing=True))
        proposal=await db.scalar(select(FinancialCaseProposal).where(FinancialCaseProposal.run_id==context_run_id,
            FinancialCaseProposal.created_by==actor_id(actor),FinancialCaseProposal.cycle_id==cycle.cycle_id).with_for_update().execution_options(populate_existing=True))
        if proposal and proposal.status=='pending':
            proposal.status='cancelled'
            await log_action_tx(db,actor_id(actor),'financial_case.proposal_cancelled','financial_case_proposal',proposal.proposal_id)
        elif proposal and proposal.status=='completed':
            facts.update(state='needs_information',message='这份方案已经执行，取消不会撤回已入账记录；请说明需要更正的费用。')
            return facts
        facts['message']='已取消这份待执行方案。';return facts
    from .feedback import review_return
    if any(s.source_id in attachment_ids and s.kind=='feedback' for s in sources):
        returned=await review_return(db,cycle,actor,sources,attachment_ids,run_id,text)
        proposal=returned['proposal']
        facts.update(state='proposal' if proposal else 'needs_information',
            message=f"已根据回填整理 {len(proposal.payload['operations'])+len(proposal.payload.get('source_changes',[]))} 项可执行变更；另有 {len(returned['issues'])} 项需要补充。请先核对方案。" if proposal else '回填原件已保存，请补充下列事项，尚未修改账目。',
            issues=returned['issues'],details=returned['details'],proposal_id=proposal.proposal_id if proposal else None,
            proposal_kind='business_correction' if proposal else None,export_ready=False)
        return facts
    from .operations import wants_operation, propose
    from .order_scope import wants_order_query, refers_to_scope
    if wants_order_query(text):
        from .order_scope import query
        try:
            facts.update(await query(db,cycle,actor,text,run_id))
        except (ValueError, MonthlyCloseConflict) as exc:
            facts.update(state='needs_information',message=str(exc),export_ready=False)
        return facts
    if re.search(r'待核实清单|核实清单|待确认清单',text):
        from .feedback import create_checklist, parse_feedback
        data=await create_checklist(db,cycle,actor)
        items=parse_feedback(data,'核实清单.docx')['facts']
        facts.update(message=f'当前还有 {len(items)} 项需要处理。可以下载下方 Word 清单交给同事，填好后直接发回这里；每项会保留独立的问题、来源和答案。',
            issues=[dict(code='pending_review',message=f['description'],source_id=None,fact_key=None) for f in items],export_ready=False)
        return facts
    if wants_operation(text):
        prior=await db.scalar(select(FinancialCaseProposal).where(
            FinancialCaseProposal.run_id==context_run_id,FinancialCaseProposal.cycle_id==cycle.cycle_id,
            FinancialCaseProposal.created_by==actor_id(actor))) if context_run_id else None
        try:
            from .chat import previous_reply
            prior_reply=await previous_reply(db,cycle,actor,context_run_id) if context_run_id else None
            query_scope=(prior_reply.facts.get('order_scope') if prior_reply and prior_reply.tool=='financial_case' else None) if refers_to_scope(text) else None
            query_context_run_id=context_run_id
            if refers_to_scope(text) and not query_scope and prior and prior.payload.get('query_scope'):
                query_scope=prior.payload['query_scope']
                query_context_run_id=query_scope['run_id']
            # A referential operation with no validated actor-bound selection
            # must never expand into a whole-month correction.
            if refers_to_scope(text) and not query_scope and not prior:
                from .operations import load_context, select_orders
                if not select_orders(await load_context(db,cycle),text):
                    raise ValueError('请先列出需要处理的订单，再根据该查询生成方案；目前没有可验证的订单范围。')
            proposal=await propose(db,cycle,actor,text,run_id,(prior.payload.get('order_ids',[]) if prior else []),
                query_scope=query_scope,context_run_id=query_context_run_id)
        except (ValueError, MonthlyCloseConflict) as exc:
            facts.update(state='needs_information',message=str(exc),export_ready=False)
            return facts
        facts.update(state='proposal',proposal_kind='business_correction',proposal_id=proposal.proposal_id,
            message=f"已整理 {len(proposal.payload['operations'])} 项变更。请核对客人、房间和修改影响，确认后执行。",
            details=proposal.payload['details'],export_ready=False)
        return facts
    from .semantic import interpret
    from app.models.monthly_close_control import MonthlyCloseMessage, MonthlyCloseRun
    context_run=await db.get(MonthlyCloseRun,context_run_id) if context_run_id else None
    history_bound=context_run.started_at if context_run and context_run.actor_id==actor_id(actor) and context_run.cycle_id==cycle.cycle_id else None
    # Include actual replies, not just user prompts: "these" refers to the result
    # the administrator has seen. Chronological order is stable across retries.
    history_rows=(await db.execute(select(MonthlyCloseMessage.role,MonthlyCloseMessage.content_redacted).join(
        MonthlyCloseRun,MonthlyCloseRun.run_id==MonthlyCloseMessage.run_id).where(
        MonthlyCloseRun.cycle_id==cycle.cycle_id,MonthlyCloseRun.actor_id==actor_id(actor),
        MonthlyCloseMessage.role.in_(['user','assistant']),MonthlyCloseRun.run_id!=run_id,
        MonthlyCloseRun.started_at<=history_bound if history_bound else True).order_by(
        MonthlyCloseRun.started_at.desc(),MonthlyCloseMessage.created_at.desc(),MonthlyCloseMessage.message_id.desc()).limit(16))).all()
    previous_texts=[('助理上次回答：' if role=='assistant' else '用户：')+content for role,content in reversed(history_rows) if content]
    from .ledger_query import is_scope_followup
    from .chat import previous_reply
    prior_reply=await previous_reply(db,cycle,actor,context_run_id)
    prior_scope=(prior_reply.facts.get('ledger_scope') or {}) if prior_reply and prior_reply.tool=='financial_case' and is_scope_followup(text) else {}
    semantic=await interpret(text,cycle.billing_month,previous_texts,[source_payload(s) for s in sources])
    # A referential filter applies to the exact previous read scope. A newly
    # named category or report still goes through normal interpretation.
    if prior_scope and not re.search(r'保洁|打扫|洗涤|水电|水费|电费|工资|物业|利润|收入|银行|投资|借款|退款|税费|社保|供应商|实付',text):
        from .semantic import Decision
        semantic=Decision(action='ledger',months=_months(text,cycle.billing_month,sources) if re.search(r'\d月|\d{4}-\d{2}',text) else prior_scope['months'],categories=prior_scope['categories'])
    if semantic and semantic.months and not re.search(r'累计|开业|从开始|从开|全部月份',text):
        facts['report_months']=semantic.months
    if semantic and semantic.action=='clarify':
        facts.update(state='needs_information',message=semantic.question)
        return facts
    if semantic and semantic.action in ('interpret','plan') and semantic.interpretation:
        interpretation=semantic.interpretation.model_dump(exclude_none=True)
        if interpretation.get('category') not in (None,*CATEGORIES.keys()):
            facts.update(state='needs_information',message='这个分类暂不能作为可确认的来源解释，请明确对应收入、投资、借款或已有费用类别；本次尚未修改资料。')
            return facts
        if not interpretation.get('matching_fact_keys') and interpretation.get('independent_cost') is not True:
            interpretation.pop('matching_fact_keys',None)
        if interpretation.get('payer')=='unconfirmed': interpretation.pop('payer',None)
        keys={(r.source_id,r.sheet,r.row) for r in semantic.rows}
        selected=[(source,fact) for source in sources for fact in source.parsed.get('facts',[])
            if fact.get('kind') in ('bank_transaction','source_expense','platform_order','platform_payout','image_expense_candidate')
            and (not semantic.source_ids or source.source_id in semantic.source_ids)
            and (not keys or (source.source_id,fact.get('sheet'),fact.get('row1based',fact.get('row'))) in keys)
            and (keys or not semantic.categories or fact.get('category') in semantic.categories)
            and (keys or interpretation.get('business_month') or not semantic.months or
                 source.decisions.get(fact['key'],{}).get('business_month',fact.get('business_month')) in semantic.months)]
        if not selected or not interpretation or not (keys or semantic.source_ids):
            facts.update(state='needs_information',message='请明确要调整哪份资料或哪几行；当前尚未修改记录。');return facts
        changes=[dict(fact_key=f['key'],decision=interpretation) for _,f in selected]
        try:
            validate_decisions(sources,changes)
        except (ValueError, MonthlyCloseConflict) as exc:
            facts.update(state='needs_information',message=f'{exc}，请补充具体来源；本次尚未生成方案。')
            return facts
        current=await match_case(db,cycle,sources)
        proposal=FinancialCaseProposal(proposal_id='FCP-'+uuid4().hex[:20].upper(),cycle_id=cycle.cycle_id,
            run_id=run_id,created_by=actor_id(actor),snapshot_hash=current['snapshot_hash'],
            payload=dict(type='interpretation',changes=changes),status='pending')
        db.add(proposal);await db.flush()
        labels={'category':'分类','business_month':'业务月份','payer':'费用承担方','paid_by':'实际付款人','include':'是否计入','note':'依据','matching_fact_keys':'对应依据','room_ref':'房间','expense_date':'费用日期','date_basis':'记账日期口径','candidate_confirmed':'图片金额已核实','discount_allocation':'优惠分摊规则','independent_cost':'已核实为独立费用','owner_distribution_basis':'业主分成口径'}
        values={**CATEGORIES,'company':'公司','owner':'业主','unconfirmed':'待核实','source_date':'使用原始日期','month_end_accrual':'按业务月末归集，保留原付款日','proportional':'按各房原金额比例分摊','net':'扣费后净额','gross':'扣费前毛额'}
        description='；'.join(f"{labels.get(k,'说明')}：{values.get(v,v) if isinstance(v,str) else ('是' if v is True else '否' if v is False else str(len(v))+' 条原始依据')}" for k,v in interpretation.items())
        facts.update(state='proposal',proposal_id=proposal.proposal_id,proposal_kind='interpretation',message=f'将按你的说明调整 {len(changes)} 条来源的分类或归属。请先核对范围与解释；确认后再重算报告，尚未记入支出。')
        facts['details']=[dict(label=f"{source.filename} · {fact.get('sheet')} 第 {fact.get('row1based',fact.get('row'))} 行 · {fact.get('amount','')} 元",value=description) for source,fact in selected]
        return facts
    # Explicit, narrow bulk clarification: source types + payer stated in this turn.
    # A payment statement never establishes who ultimately bears the expense.
    # Mixed owners/categories need the normal, row-bound interpretation preview.
    stated_payers = {party for label,party in [('公司','company'),('业主','owner')]
                     if re.search(label+r'承担',text)}
    payer = next(iter(stated_payers)) if len(stated_payers)==1 else None
    simple_bulk = re.fullmatch(
        r'(?:(?:本月|这些)|(?:(?P<year>20\d{2})[-年])?(?P<month>0?[1-9]|1[0-2])月?)?'
        r'(?:水电|水费|电费|物业费?)(?:(?:和|、|及|与)(?:水费|电费|物业费?))*'
        r'(?:费用)?(?:全部|均|都|都是)?(?:由)?(?:公司|业主)承担(?:的)?[。！!]?',text.strip())
    if payer and simple_bulk:
        # This shortcut saves an explanation immediately, so scope must come
        # only from the literal instruction, never a model-inferred month.
        bulk_month = (f"{simple_bulk['year'] or cycle.billing_month[:4]}-{int(simple_bulk['month']):02d}"
                      if simple_bulk['month'] else cycle.billing_month)
        facts['report_months'] = [bulk_month]
        cats={c for c,word in [('water','水费'),('electricity','电费'),('property','物业')] if word in text}
        if '水电' in text: cats.update(['water','electricity'])
        changes=[dict(fact_key=f['key'],decision=dict(payer=payer,note=text[:1000])) for s in sources
                 for f in s.parsed.get('facts',[]) if f.get('kind')=='source_expense' and f.get('category') in cats
                 and s.decisions.get(f['key'],{}).get('business_month',f.get('business_month')) == bulk_month]
        if changes:
            await apply_decisions(db,cycle,actor,changes)
            sources=await load_sources(db,cycle.cycle_id)
            facts['details'].append(dict(label='已记录你的说明',value=f'{len(changes)} 条费用的承担方已明确。记账前仍会展示具体方案。'))
    matched = await match_case(db,cycle,sources,facts['report_months'])
    facts['issues'] = matched['issues']
    rows=matched['ledger']
    total=sum((e.amount for e in rows if not e.is_deleted),Decimal(0))
    grouped=defaultdict(Decimal)
    for e in rows:
        if not e.is_deleted: grouped[e.category.value]+=e.amount
    facts['metrics'] = [dict(label='已保存来源',value=str(len(sources))),dict(label='已对应系统记录',value=str(len(matched['matches']))),
                       dict(label='可新增支出',value=str(len(matched['operations']))),dict(label='本月系统已记支出',value=f'{total:.2f} 元')]
    selection={'months':facts['report_months']}
    if semantic:
        wanted_rows={(r.source_id,r.sheet,r.row) for r in semantic.rows}
        selection=dict(source_ids=semantic.source_ids,
            categories=[{'property':'property_fee','cleaning_supplier':'cleaning_supplier_cost','laundry_supplier':'laundry_supplier_cost'}.get(c,c) for c in semantic.categories],
            fact_keys=[f['key'] for source in sources for f in source.parsed.get('facts',[]) if
                (source.source_id,f.get('sheet'),f.get('row1based',f.get('row'))) in wanted_rows],months=facts['report_months'])
        if 'utility' in selection['categories']:
            selection['categories']=[c for c in selection['categories'] if c!='utility']+['water','electricity','utilities']
    if (semantic and semantic.action=='plan') or re.search(r'生成记账方案|补齐费用|补齐支出|生成费用方案',text):
        matched['operations']=select_operations(matched['operations'],selection)
        if not matched['operations']:
            facts.update(state='needs_information',message='当前没有可直接新增的支出。已存在的记录不会重复记账；请先处理下方承担方、房间或月份问题。');return facts
        if not run_id: raise ValueError('生成方案需要持久化聊天记录')
        proposal=FinancialCaseProposal(proposal_id='FCP-'+uuid4().hex[:20].upper(),cycle_id=cycle.cycle_id,
            run_id=run_id,created_by=actor_id(actor),snapshot_hash=matched['snapshot_hash'],payload=dict(operations=matched['operations'],selection=selection),status='pending')
        db.add(proposal);await db.flush()
        facts.update(state='proposal',proposal_id=proposal.proposal_id,proposal_kind='posting',
            message=f"这份方案将新增 {len(matched['operations'])} 条支出，共 {sum((Decimal(o['amount']) for o in matched['operations']),Decimal(0)):.2f} 元。请核对具体记录后确认执行；其余问题继续保留。")
        room_names = dict((await db.execute(select(Room.room_id,Room.room_name))).all())
        evidence = {(s.source_id,f['key']):(s.filename,f) for s in sources for f in s.parsed['facts']}
        for op in matched['operations']:
            filename, source_fact = evidence[(op['source_id'],op['fact_key'])]
            payment = {'company':'公司已支付','owner':'业主已支付'}.get(op.get('paid_by'),'实际付款方待核实')
            detail = (f"{op['amount']} 元，{'公司' if op['payer']=='company' else '业主'}承担；{payment}。"
                      f"来源：{filename} · {source_fact.get('sheet','原表')} 第 {source_fact.get('row1based',source_fact.get('row'))} 行")
            if op.get('date_basis')=='month_end_accrual':
                detail += '；按业务月末归集，原付款日 '+str(op.get('payment_date') or '待核实')
            facts['details'].append(dict(label=f"{op['expense_date']} · {room_names.get(op['room_id']) or op['room_id'] or '公共费用'} · {EXPENSE_CATEGORY_LABELS[ExpenseCategory(op['category'])]}",value=detail))
        return facts
    if (semantic and semantic.action=='ledger') or (re.search(r'保洁|打扫|续住',text) and re.search(r'多少|费用|金额|钱|支出',text)):
        categories=semantic.categories if semantic and semantic.categories else (['cleaning'] if re.search(r'保洁|打扫|续住',text) else list(ExpenseCategory._value2member_map_))
        categories=[{'cleaning_supplier':'cleaning_supplier_cost','laundry_supplier':'laundry_supplier_cost','property':'property_fee'}.get(c,c) for c in categories]
        if 'utility' in categories:
            categories=[c for c in categories if c!='utility']+['water','electricity','utilities']
        unsupported=[c for c in categories if c not in ExpenseCategory._value2member_map_]
        if unsupported:
            facts.update(state='needs_information',message='这类费用需要按银行流水和已确认业务月份核算，不能用系统台账的空白当作零。请说“生成利润报告”查看工资等实际支出与待确认项。')
            return facts
        selected_months=facts['report_months']
        selected_rows=list((await db.scalars(select(Expense).where(Expense.is_deleted.is_(False),Expense.category.in_(
            [ExpenseCategory(c) for c in categories if c in ExpenseCategory._value2member_map_])))).all())
        selected_rows=[e for e in selected_rows if e.expense_date.strftime('%Y-%m') in selected_months]
        amount=sum((e.amount for e in selected_rows),Decimal(0))
        scope='、'.join(selected_months)
        label='、'.join(EXPENSE_CATEGORY_LABELS[ExpenseCategory(c)] for c in categories if c in ExpenseCategory._value2member_map_)
        from .ledger_query import summarize
        facts['message'],facts['details'],facts['ledger_scope']=await summarize(db,selected_rows,text,previous_texts,selected_months,label,prior_scope)
        facts['ledger_scope']['categories']=categories
        facts['metrics']=[]
        return facts
    if (semantic and semantic.action=='report') or re.search(r'利润|收入|流水|到账|累计|报表|报告|开业|这些.*多少',text):
        report, facts['report_snapshot'] = await report_snapshot(db, cycle, sources, facts['report_months'], rows)
        facts['details'].append(dict(label='报表口径',value='银行收付、业务月份、平台净结算和业主标准服务费分别核对。未确认部分不作为最终利润。'))
        # Reporting module owns arithmetic; case response only displays its evidence.
        facts['metrics'] += report.get('chat_metrics',[])
        facts['details'] += report.get('chat_details',[])
        # Report issues are their own complete scope; appending matching issues
        # before them could consume the chat limit and hide the report's tail.
        facts['issues'] = [{k:i.get(k) for k in ('code','message','source_id','fact_key')} for i in report.get('chat_issues',[])]
        facts['message']=report.get('message','已生成月度与累计核对报告。仍有未明确的分类、业务月份或重复关系；当前报表是暂算结果，可以导出查看每笔来源。')
        return facts
    for source in sources:
        for fact in source.parsed.get('facts',[]):
            if fact.get('kind')=='image_text':
                facts['details'].append(dict(label=f'{source.filename} · 图片识别文字（待核实）',value=fact.get('text','')[:2000] or '未提取到文字'))
    facts['message']=f"已整理 {len(sources)} 份资料，找到 {len(matched['matches'])} 条系统对应记录，{len(matched['operations'])} 条支出具备补齐条件。"
    if facts['issues']: facts['message']+='其余需要确认的内容已列出，原件保存不代表费用已入账。'
    if any(s.kind=='cleaning' for s in sources):
        facts['details'].append(dict(label='保洁记录与费用',value='打扫原件已接入原有核对流程。直接说“核对保洁记录”可查看缺失、重复和费用方案。'))
    return facts


async def respond(db, cycle, actor, text, attachment_ids, context_run_id=None, *, run_id=None):
    facts = await _respond(db,cycle,actor,text,attachment_ids,context_run_id,run_id=run_id)
    # The full source names and report remain available in the archive/export.
    # Keep long filenames and long month windows inside the durable chat schema.
    for detail in facts['details']:
        for key,limit in (('label',160),('value',2000)):
            if len(detail[key])>limit: detail[key]=detail[key][:limit-1]+'…'
    if len(facts['metrics'])>100:
        facts['metrics']=facts['metrics'][:99]+[dict(label='完整月份结果',value='请下载报表查看全部月份')]
    if len(facts['issues'])>500:
        remaining=len(facts['issues'])-499
        facts['issues']=facts['issues'][:499]+[_issue('more_issues',f'另有 {remaining} 项，请下载完整报告查看全部来源。')]
    if len(facts['details'])>500:
        remaining=len(facts['details'])-499
        facts['details']=facts['details'][:499]+[dict(label='其余明细',value=f'另有 {remaining} 条，完整报告保留全部记录。')]
    return facts
