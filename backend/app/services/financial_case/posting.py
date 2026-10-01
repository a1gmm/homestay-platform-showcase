"""Read-only bank-to-expense candidates; execution belongs to the case service.

Cash evidence remains immutable. A confirmed interpretation is necessary but does
not bypass date, payer, lineage, or existing standard-service-fee checks.
"""
from __future__ import annotations

import calendar
import hashlib
from collections import defaultdict
from datetime import date
from decimal import Decimal, InvalidOperation

from app.models.expense import ExpenseCategory, EXPENSE_CATEGORY_LABELS
from app.services.monthly_close.operating_expenses import build_room_aliases

BANK_EXPENSE_CATEGORIES = {
    **{key: ExpenseCategory(key) for key in (
        'payroll', 'social_insurance', 'bank_fee', 'tax', 'rent',
        'operating_expense', 'supplies', 'maintenance', 'water', 'electricity',
        'cleaning_supplier_cost', 'laundry_supplier_cost',
    )},
    'operatingexpense': ExpenseCategory.operating_expense,
    'cleaning_supplier': ExpenseCategory.cleaning_supplier_cost,
    'laundry_supplier': ExpenseCategory.laundry_supplier_cost,
    'property': ExpenseCategory.property_fee,
    'utility': ExpenseCategory.utilities,
}
NON_OPERATING = {'operating_income', 'revenue', 'ota', 'capital', 'capital_return',
                 'loan', 'loan_repayment', 'internal_transfer', 'owner_distribution'}


def _value(value):
    return getattr(value, 'value', value)


def _amount(value):
    try:
        amount = Decimal(str(value))
        if not amount.is_finite() or amount <= 0 or amount >= Decimal('100000000'):
            return None
        # Numeric(10,2) must not round evidence silently.
        return amount if amount == amount.quantize(Decimal('.01')) else None
    except (InvalidOperation, ValueError, TypeError):
        return None


def _date(value):
    try:
        return date.fromisoformat(value) if isinstance(value, str) and len(value) == 10 else None
    except ValueError:
        return None


def _posting_day(fact, decision):
    cash = _date(fact.get('date'))
    month = decision.get('business_month', fact.get('business_month'))
    first = _date(str(month) + '-01')
    if not cash or not first:
        return None, month, cash, None
    if decision.get('expense_date'):
        day = _date(decision['expense_date'])
        basis = 'explicit_business_date'
    elif decision.get('date_basis') == 'month_end_accrual':
        day = date(first.year, first.month, calendar.monthrange(first.year, first.month)[1])
        basis = 'month_end_accrual'
    elif cash.strftime('%Y-%m') == month:
        day, basis = cash, 'payment_date_in_business_month'
    else:
        day, basis = None, None
    if day and day.strftime('%Y-%m') != month:
        day = None
    return day, month, cash, basis


async def build_bank_operations(db, cycle, sources, existing_rows, rooms, existing_operations):
    """Return candidates only; inputs and database are never mutated.

    ``existing_rows`` must contain the cycle's entire ledger including tombstones;
    ``existing_operations`` are the source-expense candidates generated first.
    Confirmed matching_fact_keys form undirected evidence groups. Source detail
    takes precedence over a linked bank payment, even while that detail is blocked.
    """
    operations, issues, matches = [], [], []
    room_map = {r.room_id: r for r in rooms if not getattr(r, 'is_deleted', False)}
    aliases = build_room_aliases([(r.room_id, r.room_name) for r in room_map.values()])
    facts = {}
    for source in sorted(sources, key=lambda s: s.source_id):
        for fact in source.parsed.get('facts', []):
            facts.setdefault(fact['key'], (source, fact, (source.decisions or {}).get(fact['key'], {})))
    parent = {key: key for key in facts}

    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def issue(code, message, source, fact):
        issues.append(dict(code=code, message=message, source_id=source.source_id, fact_key=fact['key']))

    invalid_links = set()
    for key, (source, fact, decision) in facts.items():
        if decision.get('status') != 'confirmed':
            continue
        for other in decision.get('matching_fact_keys') or []:
            if other not in facts:
                invalid_links.add(key)
                issue('missing_linked_evidence', '关联的来源证据已不存在，请重新确认对应关系。', source, fact)
            else:
                parent[find(other)] = find(key)
    groups = defaultdict(list)
    for key in facts:
        groups[find(key)].append(key)
    active = [e for e in existing_rows if not e.is_deleted]
    seen = set()
    for key in sorted(facts):
        source, fact, decision = facts[key]
        if fact.get('kind') != 'bank_transaction':
            continue
        confirmed = decision.get('status') == 'confirmed'
        if confirmed and decision.get('include') is False:
            continue
        category_key = decision.get('category', fact.get('category')) if confirmed else fact.get('category')
        if category_key in NON_OPERATING:
            continue
        if fact.get('direction') != 'out':
            issue('bank_not_expense_outflow', '银行收款或方向不明不能新增经营支出。', source, fact)
            continue
        if not confirmed or not decision.get('category'):
            issue('bank_category_unconfirmed', '银行业务分类需要明确确认后才能生成支出方案。', source, fact)
            continue
        category = BANK_EXPENSE_CATEGORIES.get(category_key)
        if category is None:
            issue('bank_expense_category_unsupported', '这条银行记录尚未明确为可记账的经营费用类别。', source, fact)
            continue
        if decision.get('payer') not in ('company', 'owner'):
            issue('payer_unconfirmed', '请确认这笔银行支出由公司还是业主承担；付款账户不代表费用承担方。', source, fact)
            continue
        amount = _amount(fact.get('amount'))
        if amount is None:
            issue('invalid_expense_amount', '金额必须为可精确保存的正数，退款、零金额及无效金额需单独处理。', source, fact)
            continue
        day, month, cash, basis = _posting_day(fact, decision)
        if day is None or month != cycle.billing_month:
            issue('expense_period_unknown', '请确认业务日期或按业务月末归集，并在对应业务月份处理；不能用付款日替代跨月业务日期。', source, fact)
            continue
        reference = decision.get('room_ref', fact.get('room_ref'))
        room_id = aliases.get(reference) or (reference if reference in room_map else None)
        if reference and not room_id:
            issue('room_unmapped', '指定房间无法唯一对应系统房间，请先核对房间。', source, fact)
            continue
        owner_id = getattr(room_map.get(room_id), 'owner_id', None) if decision['payer'] == 'owner' else None
        if decision['payer'] == 'owner' and (room_id is None or owner_id is None):
            issue('owner_missing', '业主承担的费用必须明确房间及该房绑定的业主。', source, fact)
            continue
        group = groups[find(key)]
        if invalid_links.intersection(group):
            continue
        details = [other for other in group if facts[other][1].get('kind') != 'bank_transaction']
        if details:
            linked_operations = [op for op in existing_operations if op['fact_key'] in details]
            ids = [op['expense_id'] for op in linked_operations]
            for e in active:
                if any((e.notes or '').endswith(':' + other) for other in details):
                    ids.append(e.expense_id)
            # Linkage says these are the same business cost; a blocked detail must
            # never be bypassed by posting its cash evidence as a new expense.
            matches.append(dict(source_id=source.source_id, fact_key=key,
                                matching_fact_keys=sorted(details), expense_ids=sorted(set(ids))))
            issue('linked_bank_evidence_suppressed', '银行付款已关联来源明细，仅由明细生成费用；银行证据不重复记账。', source, fact)
            continue
        bank_group = [other for other in group if other != key and not (
            facts[other][2].get('status') == 'confirmed' and facts[other][2].get('include') is False)]
        if bank_group:
            # A link between two cash payments can mean split payments, not a
            # duplicate. Without a separate duplicate decision, block both.
            issue('linked_bank_group_unresolved', '多笔银行款项已关联，请明确拆分或排除重复记录后再生成费用。', source, fact)
            continue
        expense_id = 'FC-' + hashlib.sha256((source.source_id + ':' + key + ':' + cycle.cycle_id).encode()).hexdigest()[:17].upper()
        tombstone = next((e for e in existing_rows if e.expense_id == expense_id and e.is_deleted), None)
        if tombstone:
            issue('previous_expense_voided', '这条来源曾记账后作废，请核实作废原因，不能重新自动新增。', source, fact)
            continue
        exact = [e for e in active if e.expense_id == expense_id or
                 (e.room_id == room_id and e.expense_date == day and _value(e.category) == category.value and e.amount == amount)]
        if exact:
            if len(exact) != 1:
                issue('duplicate_ledger_expense', '系统存在多条对应费用，请先核实重复记录。', source, fact)
            elif _value(exact[0].payer) != decision['payer'] or exact[0].owner_id != owner_id:
                issue('ledger_payer_conflict', '已有费用与本次承担方或业主不一致，不能重复新增。', source, fact)
            elif exact[0].is_service_fee:
                issue('supplier_service_fee_overlap', '对应记录是系统标准服务费，需核实与供应商实际成本的关系，不能重复记账。', source, fact)
            elif (exact[0].expense_date != day or _value(exact[0].category)!=category.value
                  or exact[0].amount!=amount or exact[0].room_id!=room_id):
                issue('source_expense_changed', '这条来源已记入其他日期、分类或房间，请先更正已有费用，不能重新记账。', source, fact)
            elif decision.get('paid_by') in ('company','owner') and _value(getattr(exact[0],'paid_by',None))!=decision['paid_by']:
                issue('ledger_payment_conflict', '已有费用的实际付款人与本次说明不同，请先更正付款依据。', source, fact)
            else:
                matches.append(dict(source_id=source.source_id, fact_key=key, expense_ids=[exact[0].expense_id]))
            continue
        ambiguous_detail = False
        for _, other, other_decision in facts.values():
            if other.get('kind') not in ('source_expense','image_expense_candidate'):
                continue
            other_confirmed = other_decision.get('status') == 'confirmed'
            if other_confirmed and other_decision.get('include') is False:
                continue
            effective = other_decision if other_confirmed else {}
            if decision.get('independent_cost') is True or effective.get('independent_cost') is True:
                continue
            other_category = effective.get('category', other.get('category'))
            other_month = effective.get('business_month', other.get('business_month'))
            compatible_category = BANK_EXPENSE_CATEGORIES.get(other_category) == category or (
                category_key in ('utility','water','electricity') and other_category in ('utility','water','electricity')
                and 'utility' in (category_key,other_category))
            if compatible_category and other_month == month:
                other_ref = effective.get('room_ref', other.get('room_ref'))
                other_room = aliases.get(other_ref) or (other_ref if other_ref in room_map else None)
                if room_id is None or other_room is None or room_id == other_room:
                    ambiguous_detail = True
                    break
        if ambiguous_detail:
            issue('possible_duplicate_business_evidence', '同业务月已有同类来源费用，可能对应本次汇总付款；请确认对应关系或明确为独立费用，不能另按付款日重复新增。', source, fact)
            continue
        signature = (room_id, day.isoformat(), category.value, amount)
        duplicate_op = any((op.get('room_id'), op['expense_date'], op['category'], _amount(op['amount'])) == signature
                           for op in existing_operations)
        if signature in seen or duplicate_op:
            issue('duplicate_source_expense', '其他来源已有同房、同日、同类、同金额费用，请确认对应关系。', source, fact)
            continue
        seen.add(signature)
        operations.append(dict(source_id=source.source_id, fact_key=key, room_id=room_id,
            expense_date=day.isoformat(), payment_date=cash.isoformat(), business_month=month,
            date_basis=basis, category=category.value, amount=format(amount, '.2f'),
            payer=decision['payer'], paid_by=decision.get('paid_by') if decision.get('paid_by') in ('company', 'owner') else None,
            owner_id=owner_id, expense_id=expense_id,
            description=f"{EXPENSE_CATEGORY_LABELS[category]} · 银行来源第 {fact.get('row', fact.get('row1based', '?'))} 行"))
    return dict(operations=operations, issues=issues, matches=matches)
