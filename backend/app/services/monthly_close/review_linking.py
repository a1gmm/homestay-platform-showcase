"""Read-only hints that turn scattered review cards into one plain question.

Nothing here decides money. It links evidence the administrator would otherwise
have to cross-reference by hand, and pre-checks which offline orders the
existing "已按应收房费足额收到" executor would accept, so a batch button never
offers a command that the executor rejects.
"""
from __future__ import annotations

import re
from datetime import date
from typing import Any, Iterable

RECEIPT_COMMAND = "已按应收房费足额收到"


def normalize_name(name: str | None) -> str:
    return re.sub(r"[^\w]", "", name or "").replace("_", "")


def is_placeholder_name(name: str | None) -> bool:
    cleaned = normalize_name(name)
    return len(cleaned) <= 1 or bool(re.fullmatch(r"\d+", cleaned))


def _overlaps(a_in: date, a_out: date, b_in: date, b_out: date) -> bool:
    return a_in < b_out and b_in < a_out


def find_replaced_platform_orders(ota_facts: Iterable[dict], platform_orders: Iterable[dict],
                                  offline_orders: Iterable[dict]) -> list[dict]:
    """A billed platform order whose system copy was deleted or cancelled, while
    the same guest has active offline orders over the same nights."""
    by_pid: dict[str, list[dict]] = {}
    for order in platform_orders:
        by_pid.setdefault(order["platform_order_id"], []).append(order)
    offline = list(offline_orders)
    links = []
    for fact in ota_facts:
        candidates = by_pid.get(fact["platform_order_id"], [])
        if not candidates or any(o["active"] for o in candidates):
            continue
        original = candidates[0]
        name = normalize_name(original["guest_name"])
        if not name:
            continue
        replacements = [o for o in offline if normalize_name(o["guest_name"]) == name
                        and _overlaps(o["check_in"], o["check_out"], original["check_in"], original["check_out"])]
        if not replacements:
            continue
        links.append({
            "platform_order_id": fact["platform_order_id"], "source_id": fact["source_id"],
            "fact_key": fact["fact_key"], "bill_amount": fact["amount"],
            "guest_name": original["guest_name"], "original_order_id": original["order_id"],
            "check_in": str(original["check_in"]), "check_out": str(original["check_out"]),
            "offline": [{"order_id": o["order_id"], "room": o["room"], "amount": o["amount"]}
                        for o in sorted(replacements, key=lambda o: o["order_id"])],
        })
    return links


def same_guest_same_stay(offline_orders: Iterable[dict]) -> dict[str, list[str]]:
    groups: dict[tuple, list[str]] = {}
    for o in offline_orders:
        name = normalize_name(o["guest_name"])
        if name and not is_placeholder_name(o["guest_name"]):
            groups.setdefault((name, o["check_in"], o["check_out"]), []).append(o["order_id"])
    return {oid: sorted(set(ids) - {oid}) for ids in groups.values() if len(ids) > 1 for oid in ids}


async def load_review_links(db, cycle, sources, offline: dict) -> dict[str, Any]:
    from sqlalchemy import select
    from app.models.order import Order, OrderStatus

    review_ids = [r["order_id"] for r in offline.get("rows", []) if r.get("needs_review")]
    rooms = {r["order_id"]: r["room"] for r in offline.get("rows", [])}
    amounts = {r["order_id"]: r["amount"] for r in offline.get("rows", [])}
    offline_orders = []
    if review_ids:
        for o in await db.scalars(select(Order).where(Order.order_id.in_(review_ids))):
            offline_orders.append(dict(order_id=o.order_id, guest_name=o.guest_name, check_in=o.check_in_date,
                                       check_out=o.check_out_date, room=rooms.get(o.order_id), amount=amounts.get(o.order_id)))
    ota_facts = [dict(source_id=s.source_id, fact_key=f["key"], platform_order_id=f["platform_order_id"], amount=f.get("amount"))
                 for s in sources if s.kind == "ota" for f in s.parsed.get("facts", []) if f.get("platform_order_id")]
    pids = {f["platform_order_id"] for f in ota_facts}
    platform_orders = []
    if pids:
        for o in await db.scalars(select(Order).where(Order.platform_order_id.in_(pids))):
            platform_orders.append(dict(order_id=o.order_id, platform_order_id=o.platform_order_id,
                                        guest_name=o.guest_name, check_in=o.check_in_date, check_out=o.check_out_date,
                                        active=not o.is_deleted and o.order_status not in (OrderStatus.cancelled, OrderStatus.rescheduled)))
    return {
        "replaced": find_replaced_platform_orders(ota_facts, platform_orders, offline_orders),
        "placeholder": sorted(o["order_id"] for o in offline_orders if is_placeholder_name(o["guest_name"])),
        "same_guest": same_guest_same_stay(offline_orders),
        "guest_names": {o["order_id"]: o["guest_name"] for o in offline_orders},
    }


async def receipt_batch_eligible(db, cycle, order_ids: Iterable[str]) -> list[str]:
    """Orders the existing confirm-receipt executor accepts right now, one by one."""
    from app.services.financial_case import operations

    ids = sorted(set(order_ids))
    if not ids:
        return []
    ctx = await operations.load_context(db, cycle)
    eligible = []
    for order_id in ids:
        try:
            operations.plan(ctx, cycle, f"订单 {order_id} {RECEIPT_COMMAND}")
        except ValueError:
            continue
        eligible.append(order_id)
    return eligible


def receipt_batch_command(order_ids: Iterable[str]) -> str:
    return f"订单 {' '.join(sorted(order_ids))} {RECEIPT_COMMAND}"


PAYER_PHRASES = {("owner", "company"): "公司垫付，业主承担",
                 ("company", "company"): "公司支付，公司承担",
                 ("owner", "owner"): "业主支付，业主承担"}


def uniform_payer_choice(decision_sets: Iterable[dict]) -> tuple[str, str] | None:
    """The single (payer, paid_by) used for every confirmed utility row, if any."""
    choices = {(d.get("payer"), d.get("paid_by")) for decisions in decision_sets for d in decisions.values()
               if d.get("status") == "confirmed" and d.get("payer") in ("company", "owner")}
    if len(choices) != 1:
        return None
    choice = next(iter(choices))
    return choice if choice in PAYER_PHRASES else None


def previous_month(month: str) -> str:
    year, number = map(int, month.split("-"))
    return f"{year - (number == 1)}-{(number - 2) % 12 + 1:02d}"


async def previous_month_payer_default(db, cycle) -> dict | None:
    from sqlalchemy import select
    from app.models.monthly_close import MonthlyCloseCycle
    from app.services.financial_case.service import load_sources

    month = previous_month(cycle.billing_month)
    prior = await db.scalar(select(MonthlyCloseCycle).where(MonthlyCloseCycle.billing_month == month))
    if prior is None:
        return None
    utility = [s.decisions or {} for s in await load_sources(db, prior.cycle_id) if s.kind == "utility"]
    choice = uniform_payer_choice(utility)
    if choice is None:
        return None
    return {"month": month, "payer": choice[0], "paid_by": choice[1], "phrase": PAYER_PHRASES[choice],
            "count": sum(len(d) for d in utility)}


def utility_batch_command(filename: str, count: int, phrase: str) -> str:
    return f"《{filename}》这份表全部{count}笔，{phrase}"
