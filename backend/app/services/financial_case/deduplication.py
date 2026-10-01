"""Pure evidence-group suppression before canonical Expense writes."""
from collections import defaultdict
from decimal import Decimal, InvalidOperation
import re

SOURCE_KINDS = {'source_expense', 'image_expense_candidate'}
ALIASES = {'property_fee':'property', 'cleaning_supplier_cost':'cleaning_supplier',
           'laundry_supplier_cost':'laundry_supplier', 'utilities':'utility'}


def _amount(value):
    try:
        value = Decimal(str(value))
        return value if value.is_finite() and value > 0 else None
    except (ValueError, TypeError, InvalidOperation):
        return None


def _compatible(categories):
    categories = {ALIASES.get(category,category) for category in categories}
    return (len(categories) == 1 and None not in categories and 'unknown' not in categories
            or 'utility' in categories and categories <= {'utility','water','electricity'})


def deduplicate_source_operations(sources, existing_rows, operations, *, existing_matches=()):
    """Return a filtered copy, issues and explicit suppression matches.

    Confirmed links describe one business cost, with alternate source documents.
    Each document's linked rows is one representation: its total must reconcile
    with each other document (and the aggregate bank payment), without changing
    amounts. Source detail wins over image evidence unless a representation has
    already posted; conflicting or partially posted representations block writes.
    """
    from .image_facts import effective_image_fact
    facts = {}
    for source in sources:
        for original in source.parsed.get('facts', []):
            decision = (source.decisions or {}).get(original['key'], {})
            confirmed = decision.get('status') == 'confirmed'
            if confirmed and decision.get('include') is False:
                continue
            effective = decision if confirmed else {}
            fact = effective_image_fact(original,decision)
            facts[fact['key']] = dict(source_id=source.source_id, fact=fact, decision=effective,
                category=effective.get('category', fact.get('category')),
                month=effective.get('business_month', fact.get('business_month')),
                amount=_amount(fact.get('amount')), room=effective.get('room_ref', fact.get('room_ref')))
    parent = {key:key for key in facts}
    def find(key):
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key
    blocked, invalid = set(), set()
    issues, matches = [], []
    def issue(code, message, key):
        item = facts[key]
        issues.append(dict(code=code, message=message, source_id=item['source_id'], fact_key=key))
    for key,item in facts.items():
        for other in item['decision'].get('matching_fact_keys') or []:
            if other not in facts:
                invalid.add(key)
                issue('linked_cost_missing', '关联费用已排除、撤回或不存在，请重新核对对应关系。',key)
            else:
                parent[find(other)] = find(key)
            if item['decision'].get('independent_cost') is True:
                invalid.add(key)
                issue('cost_identity_conflict', '同一记录同时标记独立费用和对应同一费用，请先更正关联说明。',key)
    groups = defaultdict(list)
    for key in facts:
        groups[find(key)].append(key)
    op_by_key = {op['fact_key']:op for op in operations}
    active_ids = {e.expense_id for e in existing_rows if not e.is_deleted}
    posted = defaultdict(set)
    for key,item in facts.items():
        lineage = f"financial-case:{item['source_id']}:{key}"
        for row in existing_rows:
            if not row.is_deleted and row.notes == lineage:
                posted[key].add(row.expense_id)
    for match in existing_matches:
        if match.get('fact_key') in facts:
            posted[match['fact_key']].update(set(match.get('expense_ids', [])) & active_ids)
    row_by_id={row.expense_id:row for row in existing_rows}
    for key,item in facts.items():
        if key in op_by_key:
            item['room']=op_by_key[key].get('room_id')
        elif posted[key]:
            rooms={row_by_id[eid].room_id for eid in posted[key]}
            item['room']=next(iter(rooms)) if len(rooms)==1 else None
        elif isinstance(item['room'],str):
            composite=re.fullmatch(r'\d+-\d+-\d+-(\d{4})',item['room'])
            if composite: item['room']=composite.group(1)

    for group in groups.values():
        if len(group) < 2:
            if invalid.intersection(group): blocked.update(group)
            continue
        source_keys = [key for key in group if facts[key]['fact']['kind'] in SOURCE_KINDS]
        if not source_keys:
            continue
        def block_group(code, message):
            blocked.update(source_keys)
            for key in source_keys: issue(code,message,key)
        if invalid.intersection(group):
            block_group('linked_cost_invalid','关联费用依据不完整，请核实后再生成费用。')
            continue
        if any(facts[key]['fact']['kind'] not in SOURCE_KINDS | {'bank_transaction'} for key in group):
            block_group('linked_cost_invalid','关联中包含非费用证据，不能作为同一经营支出记账。')
            continue
        if any(facts[key]['amount'] is None for key in group):
            block_group('linked_cost_amount_conflict','关联费用缺少可核对的正数金额，不自动选取净额或补记差额。')
            continue
        if not _compatible(facts[key]['category'] for key in group):
            block_group('linked_cost_category_conflict','关联资料的费用类别不一致，请核对是否同一笔。')
            continue
        months = {facts[key]['month'] for key in group}
        if len(months) != 1 or None in months:
            block_group('linked_cost_period_conflict','关联资料的业务月份不同或尚未确认，不能重复或跨月记账。')
            continue
        if any(facts[key]['fact']['kind']=='bank_transaction' and facts[key]['fact'].get('direction')!='out' for key in group):
            block_group('linked_cost_direction_conflict','关联银行款项并非全部付款，请先核对退款或收款关系。')
            continue
        representations = defaultdict(list)
        for key in source_keys:
            representations[facts[key]['source_id']].append(key)
        totals = [sum((facts[key]['amount'] for key in keys),Decimal(0)) for keys in representations.values()]
        bank_keys = [key for key in group if facts[key]['fact']['kind']=='bank_transaction']
        if len(representations)==1 and not bank_keys:
            block_group('linked_cost_group_unresolved','同份资料内多条费用已关联，请明确重复或拆分关系后再记账。')
            continue
        if bank_keys:
            totals.append(sum((facts[key]['amount'] for key in bank_keys),Decimal(0)))
        if len(set(totals)) != 1:
            block_group('linked_cost_amount_conflict','同笔费用的银行、图片或原表合计不一致，请核实差额，不能两套入账。')
            continue
        posted_sources = {sid for sid,keys in representations.items() if any(posted[key] for key in keys)}
        bank_posted = any(posted[key] for key in bank_keys)
        posted_sets = {tuple(sorted({eid for key in representations[sid] for eid in posted[key]}))
                       for sid in posted_sources}
        if bank_posted:
            posted_sets.add(tuple(sorted({eid for key in bank_keys for eid in posted[key]})))
        if len(posted_sets) > 1:
            block_group('duplicate_posted_cost','这组关联费用已有两套系统记录，请先核实已有重复费用。')
            continue
        winner = min(posted_sources) if posted_sources else None
        if not winner and not bank_posted:
            winner = min(representations, key=lambda sid:(
                any(facts[key]['fact']['kind']=='image_expense_candidate' for key in representations[sid]), sid))
        keep = set(representations.get(winner, []))
        # A partially posted representation can only complete its own remaining
        # rows; an alternate full invoice must never bypass those existing rows.
        for key in source_keys:
            if key not in keep:
                blocked.add(key)
                matches.append(dict(source_id=facts[key]['source_id'],fact_key=key,
                    matching_fact_keys=sorted(keep or bank_keys),
                    expense_ids=sorted({eid for anchor in keep|set(bank_keys) for eid in posted[anchor]})))
                issue('linked_cost_suppressed','这笔费用已有对应资料或系统记录，仅保留一套记账依据。',key)

    # Unlinked image/source overlap must remain unresolved, even when original
    # dates or room scope differ (e.g. month-end screenshot vs per-room invoice).
    source_keys = [key for key,item in facts.items() if item['fact']['kind'] in SOURCE_KINDS]
    for key in source_keys:
        item=facts[key]
        if key not in op_by_key or item['decision'].get('independent_cost') is True:
            continue
        for other,bank in facts.items():
            if (bank['fact']['kind']!='bank_transaction' or not posted[other] or find(key)==find(other)
                or bank['decision'].get('independent_cost') is True or item['month'] is None
                or item['month']!=bank['month'] or not _compatible([item['category'],bank['category']])
                or item['room'] and bank['room'] and item['room']!=bank['room']):
                continue
            blocked.add(key)
            issue('possible_duplicate_business_evidence','同类同期银行付款已记入系统，请先对应来源或明确独立费用，不能再按明细新增。',key)
            break
    for index,left in enumerate(source_keys):
        a = facts[left]
        for right in source_keys[index+1:]:
            b = facts[right]
            if (a['source_id']==b['source_id'] or find(left)==find(right)
                or 'image_expense_candidate' not in {a['fact']['kind'],b['fact']['kind']}
                or a['month'] is None or a['month']!=b['month']
                or not _compatible([a['category'],b['category']])
                or a['room'] and b['room'] and a['room']!=b['room']
                or a['decision'].get('independent_cost') is True or b['decision'].get('independent_cost') is True):
                continue
            if not (left in op_by_key or posted[left]) or not (right in op_by_key or posted[right]):
                continue
            if posted[left] and posted[right]:
                targets=[left,right]
                code='duplicate_posted_cost'
                message='同类同期图片与原表已有两套系统费用，请先核实是否重复。'
            elif posted[left] or posted[right]:
                targets=[right if posted[left] else left]
                code='possible_duplicate_business_evidence'
                message='同类同期费用已有系统记录，请对应原件或明确这是独立费用，不能重复新增。'
            else:
                targets=[key for key in (left,right) if facts[key]['fact']['kind']=='image_expense_candidate']
                code='possible_duplicate_business_evidence'
                message='同类同期图片与原表可能属于同笔费用，请先对应原件或明确这是独立费用。'
            for key in targets:
                blocked.add(key)
                issue(code,message,key)
    return dict(operations=[op for op in operations if op['fact_key'] not in blocked],issues=issues,matches=matches)
