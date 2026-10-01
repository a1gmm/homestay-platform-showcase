"""Deterministic, read-only bank evidence extraction; classifications are suggestions.

Bank cash dates never imply the revenue recognition month. Existing profit sheets
are retained as unapproved source templates, not accepted accounting results.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.services.billing_recon.parser import load_workbook_rows

_ZERO = Decimal('0')
_HEADERS = {
    'date': {'日期', '交易日期', '记账日期'},
    'description': {'摘要', '交易摘要', '交易说明', '用途'},
    'in': {'收款金额', '收入金额', '贷方发生额', '收入'},
    'out': {'付款金额', '支出金额', '借方发生额', '支出'},
    'balance': {'余额', '账户余额', '交易余额'},
}
_BALANCE_LABEL = re.compile(r'^(?:期初|期末|上期|本期|月初|月末|上日|当日)?余额$')
_TOTAL_LABEL = re.compile(r'^(?:(?:本月|本期|当月|本日|本年|累计|\d{1,2}月份?)\s*)?(?:合计|小计|累计|总计)$')


def _text(value):
    return '' if value is None else str(value).strip()


def _cell(row, index):
    return row[index] if index is not None and index < len(row) else None


def _money(value):
    if value is None or _text(value) in {'', '-', '—'}:
        return None
    if isinstance(value, bool):
        raise ValueError('invalid amount')
    token = re.sub(r'[,，￥¥\s]', '', str(value))
    if token.startswith('(') and token.endswith(')'):
        token = '-' + token[1:-1]
    try:
        amount = Decimal(token)
    except InvalidOperation as exc:
        raise ValueError('invalid amount') from exc
    if not amount.is_finite():
        raise ValueError('invalid amount')
    return amount


def _fmt(value):
    return format(value, 'f')


def _date(value, datemode):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        from openpyxl.utils.datetime import CALENDAR_MAC_1904, CALENDAR_WINDOWS_1900, from_excel
        try:
            parsed = from_excel(value, epoch=CALENDAR_MAC_1904 if datemode else CALENDAR_WINDOWS_1900)
            return parsed.date() if isinstance(parsed, datetime) else None
        except (ValueError, OverflowError, TypeError):
            return None
    token = _text(value)
    for pattern in ('%Y-%m-%d', '%Y/%m/%d', '%Y.%m.%d', '%Y年%m月%d日', '%Y%m%d', '%Y-%m-%d %H:%M:%S', '%Y/%m/%d %H:%M:%S'):
        try:
            return datetime.strptime(token, pattern).date()
        except ValueError:
            pass
    return None


def _columns(row):
    result = {}
    for index, cell in enumerate(row):
        value = re.sub(r'\s+', '', _text(cell))
        for key, labels in _HEADERS.items():
            if value in labels and key not in result:
                result[key] = index
    return result if {'date', 'description', 'in', 'out'} <= result.keys() else None


def _category(description, direction):
    # More specific classifications precede loose expenditure/revenue terms.
    rules = (
        ('internal_transfer', r'内部划转|内部转账|账户互转|账户间划转|本公司账户'),
        ('capital_return', r'退还.*投资|退投资|退股|撤资|返还.*出资'),
        ('loan_repayment', r'还.*借款|归还.*款|偿还.*款|还贷|还.*本金'),
        ('capital', r'投资款|出资款|增资款|资本金'),
        ('loan', r'借.*款|贷款放款|借入'),
        ('owner_distribution', r'房东.*分成|业主.*分成|分红|利润分配'),
        ('payroll', r'工资|薪资|薪酬'),
        ('social_insurance', r'社保|五险一金|公积金|社会保险|养老保险|医疗保险|失业保险|工伤保险|生育保险'),
        ('bank_fee', r'手续费|银行.*扣费|账户管理费'),
        ('tax', r'税费|缴税|税款|增值税|印花税|所得税'),
        ('laundry_supplier', r'洗涤|布草清洗|洗衣'),
        ('cleaning_supplier', r'保洁|清洁服务|清扫费'),
        ('property', r'物业'),
        ('maintenance', r'维修|修理|检修'),
        ('utility', r'水费|电费|水电|燃气费|网费|宽带费'),
        ('supplies', r'采购|耗材|日用品|办公用品'),
    )
    for category, pattern in rules:
        if re.search(pattern, description):
            return category, 'suggested'
    if direction == 'in' and re.search(r'房费|营业收入|经营收入|收.*收入|携程|美团|途家|抖音', description):
        return 'operating_income', 'suggested'
    return 'unknown', 'suggested'


def _business_month(description, cash_date):
    # Require an explicit business concept next to a stated month. Cash settlement
    # wording alone (e.g. 8月到账) does not establish the underlying business month.
    match = re.search(r'(?:(20\d{2})年)?(1[0-2]|0?[1-9])月(?:份)?[^年月\d]{0,12}?(?:工资|薪资|收入|房费|社保|五险|公积金|养老保险|医疗保险|水费|电费|物业|税费|税款|增值税|保洁|洗涤|分成)', description)
    if not match or (cash_date is None and match[1] is None):
        return None
    month = int(match[2])
    year = int(match[1]) if match[1] else cash_date.year - int(month > cash_date.month)
    return f'{year:04d}-{month:02d}'


def _template(sheet, rows):
    title = next((_text(c) for row in rows[:5] for c in row if '利润' in _text(c)), None)
    if title is None:
        return None
    header = next(((i, row) for i, row in enumerate(rows[:20]) if '明细' in [_text(c) for c in row] and '金额' in [_text(c) for c in row]), None)
    if header is None:
        return None
    header_index, row = header
    labels = [_text(c) for c in row]
    label_col, amount_col = labels.index('明细'), labels.index('金额')
    entries, checks, group = [], [], []
    totals = {}
    for index, row in enumerate(rows[header_index + 1:], header_index + 2):
        label = _text(_cell(row, label_col))
        if not label:
            continue
        try:
            amount = _money(_cell(row, amount_col))
        except ValueError:
            amount = None
        entry = {'row': index, 'label': label, 'cached_amount': _fmt(amount) if amount is not None else None, 'formula': None}
        entries.append(entry)
        if label in {'收入小计', '费用小计', '分成小计'}:
            expected = sum(group, _ZERO) if all(v is not None for v in group) and group else None
            checks.append({'row': index, 'label': label, 'expected': _fmt(expected) if expected is not None else None, 'reported': entry['cached_amount'], 'matches': expected == amount if expected is not None and amount is not None else None})
            totals[label] = amount
            group = []
        elif label == '净利润':
            operands = [totals.get(key) for key in ('收入小计', '费用小计', '分成小计')]
            expected = operands[0] - operands[1] - operands[2] if all(v is not None for v in operands) else None
            checks.append({'row': index, 'label': label, 'expected': _fmt(expected) if expected is not None else None, 'reported': entry['cached_amount'], 'matches': expected == amount if expected is not None and amount is not None else None})
        else:
            group.append(amount)
    return {'sheet': sheet, 'title': title, 'kind': 'existing_profit_template', 'entries': entries, 'checks': checks, 'accounting_status': 'unapproved_source_template', 'formula_availability': 'unavailable_from_data_only_loader'}


def parse_bank_workbook(data: bytes, filename: str) -> dict | None:
    sheets, datemode = load_workbook_rows(data, filename)
    result = {'kind': 'bank', 'facts': [], 'summaries': [], 'issues': [], 'templates': []}
    bank_found = False
    for sheet, rows in sheets.items():
        header = next(((index, _columns(row)) for index, row in enumerate(rows[:40]) if _columns(row)), None)
        if header is None:
            template = _template(sheet, rows)
            if template is not None:
                result['templates'].append(template)
            continue
        bank_found = True
        header_index, columns = header
        carry_date = None
        prior_balance = None
        totals = {'in': _ZERO, 'out': _ZERO}
        fact_count, balance_checks = 0, 0

        def issue(code, row_number, **details):
            result['issues'].append({'code': code, 'sheet': sheet, 'row': row_number, **details})

        for index, row in enumerate(rows[header_index + 1:], header_index + 2):
            repeated_header = _columns(row)
            if repeated_header:
                columns = repeated_header
                carry_date = None
                continue
            if not any(_text(c) for c in row):
                carry_date = None
                continue
            raw_date = _cell(row, columns['date'])
            description = _text(_cell(row, columns['description']))
            labels = [_text(raw_date), description]
            try:
                balance = _money(_cell(row, columns.get('balance')))
            except ValueError:
                balance = None
                issue('INVALID_BANK_BALANCE', index)
            if any(_BALANCE_LABEL.fullmatch(label) for label in labels):
                if prior_balance is not None and balance is not None and prior_balance != balance:
                    issue('BANK_BALANCE_ANCHOR_MISMATCH', index, expected=_fmt(prior_balance), reported=_fmt(balance))
                prior_balance, carry_date = balance, None
                continue
            if any(_TOTAL_LABEL.fullmatch(label) for label in labels):
                carry_date = None
                continue
            parsed_date = _date(raw_date, datemode)
            if parsed_date is not None:
                carry_date = parsed_date
            elif _text(raw_date):
                carry_date = None
            amounts = {}
            bad_amount = False
            for direction in ('in', 'out'):
                try:
                    amounts[direction] = _money(_cell(row, columns[direction])) or _ZERO
                except ValueError:
                    amounts[direction] = _ZERO
                    bad_amount = True
                    issue('INVALID_BANK_AMOUNT', index, direction=direction)
            if amounts['in'] == 0 and amounts['out'] == 0:
                if balance is not None:
                    if prior_balance is not None and balance != prior_balance:
                        issue('BANK_BALANCE_WITHOUT_TRANSACTION', index, expected=_fmt(prior_balance), reported=_fmt(balance))
                    prior_balance = balance
                continue
            row_issues = []
            if bad_amount:
                row_issues.append('INVALID_BANK_AMOUNT')
            if carry_date is None:
                row_issues.append('MISSING_BANK_TRANSACTION_DATE')
            if amounts['in'] and amounts['out']:
                row_issues.append('BOTH_BANK_CASH_DIRECTIONS')
            if any(amount < 0 for amount in amounts.values()):
                row_issues.append('NEGATIVE_BANK_AMOUNT')
            if prior_balance is not None and balance is not None and not bad_amount:
                balance_checks += 1
                expected = prior_balance + amounts['in'] - amounts['out']
                if expected != balance:
                    row_issues.append('BANK_RUNNING_BALANCE_MISMATCH')
                    issue('BANK_RUNNING_BALANCE_MISMATCH', index, expected=_fmt(expected), reported=_fmt(balance))
            for code in row_issues:
                if code not in {'BANK_RUNNING_BALANCE_MISMATCH', 'INVALID_BANK_AMOUNT'}:
                    issue(code, index)
            # Never bridge an absent balance: the next known value is a fresh anchor.
            prior_balance = balance
            for direction, amount in amounts.items():
                if amount <= 0:
                    continue  # Invalid negative cash is visible in issues, never flipped.
                category, classification = _category(description, direction)
                fact_issues = list(row_issues)
                if category == 'unknown':
                    fact_issues.append('BANK_CATEGORY_UNRESOLVED')
                cash_date = carry_date.isoformat() if carry_date else None
                key_source = [sheet, index, cash_date, direction, _fmt(amount), description]
                fact = {'key': hashlib.sha256(json.dumps(key_source, ensure_ascii=False, separators=(',', ':')).encode()).hexdigest(), 'sheet': sheet, 'row': index, 'kind': 'bank_transaction', 'date': cash_date, 'business_month': _business_month(description, carry_date), 'direction': direction, 'amount': _fmt(amount), 'description': description, 'account': sheet, 'category': category, 'classification': classification, 'issues': fact_issues}
                reference = re.search(r'(?:订单号|订单编号|单号)\s*[:：]?\s*([A-Za-z0-9-]{6,})', description)
                if reference:
                    fact['source_reference'] = reference[1]
                result['facts'].append(fact)
                totals[direction] += amount
                fact_count += 1
        result['summaries'].append({'sheet': sheet, 'account': sheet, 'transaction_count': fact_count, 'cash_in': _fmt(totals['in']), 'cash_out': _fmt(totals['out']), 'net_cash': _fmt(totals['in'] - totals['out']), 'balance_checks': balance_checks, 'independent_total_verified': False})
    return result if bank_found else None
