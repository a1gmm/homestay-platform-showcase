"""Explicit order-level cost allocation shared by posting and settlement.

Ordinary tariffs remain unchanged. Only a versioned administrator-confirmed
policy can override their beneficiary; natural language and legacy payer values
alone are not evidence of an override.
"""
from app.models.expense import ExpensePayer
from app.models.order import is_owner_self_order


def confirmed_cost_bearer(metadata):
    policy = (metadata or {}).get('reconciliation_policy')
    if not isinstance(policy, dict) or policy.get('version') != 1:
        return None
    if not policy.get('confirmed_by') or not policy.get('confirmed_at'):
        return None
    if policy.get('service_cost_bearer') not in ('company', 'owner'):
        return None
    return ExpensePayer(policy['service_cost_bearer'])


def service_cost_payer(order):
    if order is None:
        return ExpensePayer.owner
    if bearer := confirmed_cost_bearer(order.metadata_):
        return bearer
    if is_owner_self_order(order):
        return ExpensePayer.company
    return confirmed_cost_bearer(order.metadata_) or ExpensePayer.owner


def checkout_cleaning_amount(order, fees):
    """Confirmed owner guests pay the normal tariff; company staff cost 30.

    Legacy self-use markers alone cannot establish an owner deduction. They
    retain their company allocation until the administrator confirms the party.
    """
    from app.core.cleaning_pricing import STAFF_CHECKOUT_CLEANING_FEE
    if order is not None and is_owner_self_order(order) and service_cost_payer(order) == ExpensePayer.company:
        return STAFF_CHECKOUT_CLEANING_FEE
    return fees.checkout_cleaning_fee


def order_acceptance_fingerprint(order, rooms):
    import json
    from hashlib import sha256
    data={'order': [order.order_id,str(order.channel),order.platform_order_id,str(order.actual_price),
        str(order.platform_commission_rate),str(order.order_status),order.is_deleted,
        (order.metadata_ or {}).get('ota_owner_revenue')],
        'rooms':sorted([[r.order_room_id,r.room_id,str(r.check_in_date),str(r.check_out_date),
            str(r.actual_price),str(r.ota_owner_revenue)] for r in rooms],key=lambda r:r[0])}
    return sha256(json.dumps(data,sort_keys=True,default=str).encode()).hexdigest()


def accepted_current_order(order, rooms, billing_month):
    confirmation=(order.metadata_ or {}).get('settlement_acceptance',{})
    return bool(confirmation.get('version')==1 and confirmation.get('confirmed_by')
        and confirmation.get('confirmed_at') and confirmation.get('billing_month')==billing_month
        and confirmation.get('basis')=='user_confirmed_current_system_figures'
        and confirmation.get('order_fingerprint')==order_acceptance_fingerprint(order,rooms))


def receipt_confirmation_fingerprint(order, rooms, payments, refunds):
    import json
    from hashlib import sha256
    from app.models.refund import RefundReason
    data = {'order': order_acceptance_fingerprint(order, rooms),
        'payments': sorted([[p.payment_id, str(p.amount), str(p.paid_at)] for p in payments
            if p.order_id == order.order_id and not p.is_deleted and not p.is_deposit]),
        'refunds': sorted([[r.refund_id, str(r.amount), str(r.refunded_at)] for r in refunds
            if r.order_id == order.order_id and not r.is_deleted and r.reason != RefundReason.deposit_return])}
    return sha256(json.dumps(data,sort_keys=True).encode()).hexdigest()


def confirmed_receipt_amount(order, rooms, payments, refunds, billing_month):
    from decimal import Decimal, InvalidOperation
    fact=(order.metadata_ or {}).get('receipt_confirmation') or {}
    if not (fact.get('version')==1 and fact.get('confirmed_by') and fact.get('confirmed_at')
            and fact.get('billing_month')==billing_month and fact.get('basis')=='administrator_confirmed_total'
            and fact.get('fingerprint')==receipt_confirmation_fingerprint(order,rooms,payments,refunds)):
        return None
    try:
        amount=Decimal(fact['amount'])
        return amount if amount.is_finite() and amount>0 else None
    except (InvalidOperation,KeyError,TypeError):
        return None
