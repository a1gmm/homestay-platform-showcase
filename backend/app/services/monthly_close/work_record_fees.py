"""Reconcile confirmed historical stayover work with the service-fee ledger.

Activity quantities are evidence, not money. Only a unique occupied order and
unambiguous ledger identity may produce an additive fee proposal. Checkout fees
continue to use service_fee_reconciliation, never this date-only work log.
"""
from datetime import date
from decimal import Decimal
from hashlib import sha256
import json

from sqlalchemy import select

from app.core.cleaning_pricing import OWNER_SELF_INSTAY_CLEANING_FEE
from app.core.datetime_helpers import today_cn
from app.models.cleaning_request import CleaningRequest
from app.models.cleaning_work_record import CleaningWorkRecord
from app.models.expense import Expense, ExpenseCategory, ExpensePayer
from app.models.order import Order, OrderStatus, is_owner_self_order
from app.models.order_room import OrderRoom
from app.models.room import Room
from app.models.settlement import OwnerSettlement, SettlementStatus
from app.services.audit import log_action_tx
from app.services.service_fees import get_service_fees


def digest(value):
    return sha256(json.dumps(value, sort_keys=True, default=str).encode()).hexdigest()


async def plan_work_fees(db, document, billing_month, *, rooms=(), service_date=None):
    """Return a read-only proposal and explicit unresolved rows, without guesses."""
    records = list(await db.scalars(select(CleaningWorkRecord).where(
        CleaningWorkRecord.document_id == document.document_id,
    ).order_by(CleaningWorkRecord.service_date, CleaningWorkRecord.room_id)))
    room_ids = {r.room_id for r in records}
    room_map = {r.room_id: r for r in await db.scalars(select(Room).where(Room.room_id.in_(room_ids)))}
    fees = await get_service_fees(db)
    expenses = list(await db.scalars(select(Expense).where(
        Expense.room_id.in_(room_ids), Expense.category == ExpenseCategory.cleaning,
    )))
    requests = list(await db.scalars(select(CleaningRequest).where(CleaningRequest.room_id.in_(room_ids))))
    stays = list((await db.execute(select(OrderRoom, Order).join(Order, OrderRoom.order_id == Order.order_id).where(
        OrderRoom.room_id.in_(room_ids), Order.is_deleted.is_(False), Order.order_status != OrderStatus.cancelled,
    ))).all())
    settlements = list(await db.scalars(select(OwnerSettlement).where(
        OwnerSettlement.billing_month == billing_month,
    )))
    # Pending drafts may become stale as new expenses arrive. Existing settlement
    # confirmation compares the live financial snapshot and refuses such drafts.
    # Recognized/paid/disputed statements require a separate correction workflow.
    locked = {s.owner_id for s in settlements if s.status != SettlementStatus.pending}
    actions, unresolved, blocked_actions = [], [], []
    for record in records:
        if rooms and record.room_id not in rooms:
            continue
        if service_date and record.service_date.isoformat() != service_date:
            continue
        if record.service_type != "instay_cleaning":
            continue
        base = dict(record_id=record.record_id, room_ref=record.room_id,
                    service_date=record.service_date.isoformat(), service_type=record.service_type,
                    quantity=record.quantity)
        def block(reason):
            unresolved.append({**base, "reason": reason})
        if record.document_sha256 != document.sha256 or record.service_date.strftime("%Y-%m") != billing_month:
            block("原表版本或月份不一致，需要重新核实")
            continue
        if record.service_date > today_cn():
            block("打扫日期尚未发生，不能提前记账")
            continue
        candidates = {order.order_id: order for leg, order in stays if leg.room_id == record.room_id
                      and leg.check_in_date <= record.service_date < leg.check_out_date}
        room = room_map.get(record.room_id)
        if len(candidates) != 1 or room is None or not room.owner_id:
            block("无法唯一对应入住订单或业主")
            continue
        order = next(iter(candidates.values()))
        from app.services.reconciliation_policy import service_cost_payer
        payer = service_cost_payer(order)
        unit = OWNER_SELF_INSTAY_CLEANING_FEE if is_owner_self_order(order) else fees.instay_cleaning_fee
        if unit <= 0:
            block("当前续住费用标准为零，不能推算历史收费")
            continue
        day_requests = [r for r in requests if r.room_id == record.room_id and r.request_date == record.service_date]
        linked_ids = {r.expense_id for r in day_requests if r.expense_id}
        matches = [e for e in expenses if e.room_id == record.room_id and
                   (e.expense_id in linked_ids or (e.expense_date == record.service_date and e.description.startswith("续住打扫")))]
        # Arbitrary/manual charges and revoked charges need a human decision.
        if any(e.is_deleted or e.order_id != order.order_id or e.payer != payer or not e.is_service_fee
               or not e.description.startswith("续住打扫") or e.amount <= 0 or e.amount % unit != 0 for e in matches):
            block("已有费用的金额、承担方、关联订单或作废状态需要核实")
            continue
        unknown = [e for e in expenses if e.room_id == record.room_id and not e.is_deleted and e not in matches
                   and not e.description.startswith("退房打扫")
                   and (e.expense_date == record.service_date or e.order_id == order.order_id)
                   and not (e.is_service_fee and e.payer == payer and e.amount == unit and any(r.expense_id == e.expense_id and r.request_date != record.service_date for r in requests))
                   and not (e.description.startswith("续住打扫") and e.is_service_fee and e.payer == payer and any(
                       r.room_id == e.room_id and r.service_date == e.expense_date and r.record_id != record.record_id
                       and r.service_type == "instay_cleaning" and e.amount == unit * r.quantity for r in records))]
        if unknown:
            block("同一订单或日期还有未明确关联的保洁费用，需排除重复计费")
            continue
        paid = sum((e.amount for e in matches), Decimal("0"))
        expected = unit * record.quantity
        if paid > expected:
            block("已有费用超过原表确认次数，应核实后单独更正")
            continue
        if paid == expected:
            continue
        # A stable ID prevents accidental duplicate creates, including a revoked
        # prior backfill. The notes retain source row and original matched fees.
        fee_id = "EXP-" + digest([record.record_id, str(expected), str(paid)])[:16].upper()
        if any(e.expense_id == fee_id for e in expenses):
            block("这项补账已存在或被作废，不能自动重新收费")
            continue
        action = {**base, "kind": "fee", "expense_id": fee_id,
                        "order_id": order.order_id, "owner_id": room.owner_id,
                        "payer": payer.value, "amount": str(expected - paid),
                        "unit_price": str(unit), "existing_amount": str(paid), "expected_amount": str(expected),
                        "document_id": document.document_id, "document_sha256": document.sha256,
                        "source_sheet": record.source_sheet, "source_row": record.source_row,
                        "matched_expense_ids": sorted(e.expense_id for e in matches),
                        "description": f"续住打扫 {room.room_name}（历史费用补齐）"}
        if room.owner_id in locked:
            block("业主本月结算已生成，需先处理结算更正")
            blocked_actions.append(action)
        else:
            actions.append(action)
    return {"actions": actions, "unresolved": unresolved,
            "pending_settlement_ids": sorted(s.settlement_id for s in settlements if s.status == SettlementStatus.pending
                and any(a["owner_id"] == s.owner_id for a in actions)),
            "blocked_actions": blocked_actions,
            "blocked_amount": str(sum((Decimal(a["amount"]) for a in blocked_actions), Decimal("0"))),
            "amount": str(sum((Decimal(a["amount"]) for a in actions), Decimal("0")))}


async def add_work_fee(db, action, actor_id):
    """Caller locks/revalidates the complete proposal and commits its audit atomically."""
    db.add(Expense(
        expense_id=action["expense_id"], category=ExpenseCategory.cleaning,
        amount=Decimal(action["amount"]), description=action["description"],
        expense_date=date.fromisoformat(action["service_date"]),
        room_id=action["room_ref"], order_id=action["order_id"], owner_id=action["owner_id"],
        payer=ExpensePayer(action["payer"]), is_service_fee=True, created_by=actor_id,
        notes=json.dumps({"source": "confirmed_cleaning_work", **action}, ensure_ascii=False),
    ))
    await db.flush()
    await log_action_tx(db, actor_id, "monthly_close.work_fee.create", "expense", action["expense_id"], after_data=action)
