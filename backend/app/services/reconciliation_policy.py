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
    if is_owner_self_order(order):
        return ExpensePayer.company
    return confirmed_cost_bearer(order.metadata_) or ExpensePayer.owner


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
