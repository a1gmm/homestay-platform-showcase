"""Read-only planning for checkout service-fee ledger reconciliation."""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date, datetime, timezone
from decimal import Decimal
from types import MappingProxyType
from typing import Mapping
from uuid import uuid4

from sqlalchemy import and_, extract, or_, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.expense import Expense, ExpenseCategory, ExpensePayer
from app.models.order import Order, OrderStatus
from app.models.order_room import OrderRoom
from app.models.room import Room
from app.services import service_fee_ledger
from app.services.audit import log_action_tx
from app.services.service_fees import get_service_fees


@dataclass(frozen=True)
class PlannedServiceFee:
    """One deterministic service-fee ledger row that should exist."""

    owner_id: str
    order_id: str
    room_id: str
    category: ExpenseCategory
    amount: Decimal
    expense_date: date
    nights: int
    stay_group_id: str | None


@dataclass(frozen=True)
class UnresolvedServiceFee:
    """A fee source that cannot be safely inferred without human review."""

    reason: str
    order_id: str | None = None
    room_id: str | None = None
    stay_group_id: str | None = None
    order_ids: tuple[str, ...] = ()
    room_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlannedServiceFeeCorrection:
    """One active ledger row whose same-month amount/date no longer matches its stay."""

    expense_id: str
    current_amount: Decimal
    current_expense_date: date
    expected: PlannedServiceFee


@dataclass(frozen=True)
class ServiceFeeReconciliationPlan:
    """Read-only reconciliation result for one owner and checkout month."""

    owner_id: str
    year: int
    month: int
    expected: tuple[PlannedServiceFee, ...]
    missing: tuple[PlannedServiceFee, ...]
    corrections: tuple[PlannedServiceFeeCorrection, ...]
    unresolved: tuple[UnresolvedServiceFee, ...]
    category_totals: Mapping[ExpenseCategory, Decimal]


@dataclass(frozen=True)
class ServiceFeeRepairResult:
    """Ledger rows created by one reconciliation application."""

    created_count: int
    corrected_count: int
    created_amount: Decimal
    category_totals: Mapping[ExpenseCategory, Decimal]


class ServiceFeeReconciliationError(RuntimeError):
    """A reconciliation plan is unsafe to apply automatically."""

    def __init__(self, plan: ServiceFeeReconciliationPlan):
        self.owner_id = plan.owner_id
        self.year = plan.year
        self.month = plan.month
        self.unresolved = plan.unresolved
        super().__init__(
            "service-fee reconciliation has unresolved entries "
            f"for owner={plan.owner_id} month={plan.year:04d}-{plan.month:02d}"
        )


async def plan_service_fee_reconciliation(
    db: AsyncSession,
    owner_id: str,
    year: int,
    month: int,
    cutoff: date | None = None,
) -> ServiceFeeReconciliationPlan:
    """Plan deterministic checkout fees without mutating the ledger.

    Active continuation segments are charged once on their unique final node.
    Rows that cannot be assigned to an owner, amount, or unique charge key are
    returned as unresolved and never converted into guessed fee rows.
    """
    checkout_in_month = and_(
        extract("year", OrderRoom.check_out_date) == year,
        extract("month", OrderRoom.check_out_date) == month,
    )
    order_checkout_in_month = and_(
        OrderRoom.check_out_date.is_(None),
        extract("year", Order.check_out_date) == year,
        extract("month", Order.check_out_date) == month,
    )
    candidate_scope = or_(checkout_in_month, order_checkout_in_month)
    if cutoff is not None:
        candidate_scope = and_(
            candidate_scope,
            or_(
                OrderRoom.check_out_date <= cutoff,
                and_(OrderRoom.check_out_date.is_(None), Order.check_out_date <= cutoff),
            ),
        )

    candidate_stmt = (
        select(OrderRoom, Order, Room)
        .join(Order, Order.order_id == OrderRoom.order_id)
        .outerjoin(Room, Room.room_id == OrderRoom.room_id)
        .where(Order.is_deleted.is_(False))
        .where(Order.order_status != OrderStatus.cancelled)
        .where(candidate_scope)
        .order_by(OrderRoom.check_out_date, OrderRoom.order_room_id)
    )
    candidate_rows = list((await db.execute(candidate_stmt)).all())

    group_ids = {order.stay_group_id for _, order, _ in candidate_rows if order.stay_group_id}
    grouped_rows: dict[str, list[tuple[OrderRoom, Order, Room | None]]] = {}
    if group_ids:
        all_group_rows = (
            await db.execute(
                select(OrderRoom, Order, Room)
                .join(Order, Order.order_id == OrderRoom.order_id)
                .outerjoin(Room, Room.room_id == OrderRoom.room_id)
                .where(Order.is_deleted.is_(False))
                .where(Order.order_status != OrderStatus.cancelled)
                .where(Order.stay_group_id.in_(group_ids))
                .order_by(OrderRoom.check_out_date, OrderRoom.order_room_id)
            )
        ).all()
        for row in all_group_rows:
            group_id = row[1].stay_group_id
            if group_id is not None:
                grouped_rows.setdefault(group_id, []).append(row)

    ordinary_rows = [row for row in candidate_rows if row[1].stay_group_id is None]
    fees = await get_service_fees(db)

    expected: list[PlannedServiceFee] = []
    unresolved: list[UnresolvedServiceFee] = []

    def in_scope(value: date | None) -> bool:
        return (
            value is not None
            and value.year == year
            and value.month == month
            and (cutoff is None or value <= cutoff)
        )

    def add_unresolved(
        reason: str,
        *,
        order: Order | None = None,
        order_room: OrderRoom | None = None,
        stay_group_id: str | None = None,
        affected_rows: list[tuple[OrderRoom, Order, Room | None]] | None = None,
    ) -> None:
        affected_rows = affected_rows or []
        if affected_rows and order is None:
            order = affected_rows[0][1]
        if affected_rows and order_room is None:
            order_room = affected_rows[0][0]
        unresolved.append(
            UnresolvedServiceFee(
                reason=reason,
                order_id=order.order_id if order is not None else None,
                room_id=order_room.room_id if order_room is not None else None,
                stay_group_id=stay_group_id,
                order_ids=tuple(sorted({row_order.order_id for _, row_order, _ in affected_rows})),
                room_ids=tuple(
                    sorted(
                        {
                            affected_order_room.room_id
                            for affected_order_room, _, _ in affected_rows
                            if affected_order_room.room_id is not None
                        }
                    )
                ),
            )
        )

    def plan_rows(
        rows: list[tuple[OrderRoom, Order, Room | None]],
        stay_group_id: str | None,
    ) -> None:
        scope_dates = [
            order_room.check_out_date or order.check_out_date
            for order_room, order, _ in rows
        ]
        final_scope_date = max((value for value in scope_dates if value is not None), default=None)
        if not in_scope(final_scope_date):
            return

        known_owners = {
            room.owner_id for _, _, room in rows if room is not None and room.owner_id
        }
        has_unknown_owner = any(room is None or room.owner_id is None for _, _, room in rows)
        if owner_id not in known_owners and not has_unknown_owner:
            return

        missing_room_row = next(
            ((order_room, order) for order_room, order, room in rows if room is None),
            None,
        )
        if missing_room_row is not None:
            order_room, order = missing_room_row
            add_unresolved(
                "missing_room",
                order=order,
                order_room=order_room,
                stay_group_id=stay_group_id,
            )
            return

        missing_owner_row = next(
            (
                (order_room, order)
                for order_room, order, room in rows
                if room.owner_id is None
            ),
            None,
        )
        if missing_owner_row is not None:
            order_room, order = missing_owner_row
            add_unresolved(
                "missing_owner",
                order=order,
                order_room=order_room,
                stay_group_id=stay_group_id,
            )
            return

        invalid_date_row = next(
            (
                (order_room, order)
                for order_room, order, _ in rows
                if order_room.check_in_date is None or order_room.check_out_date is None
            ),
            None,
        )
        if invalid_date_row is not None:
            order_room, order = invalid_date_row
            add_unresolved(
                "missing_dates",
                order=order,
                order_room=order_room,
                stay_group_id=stay_group_id,
            )
            return

        invalid_range_row = next(
            (
                (order_room, order)
                for order_room, order, _ in rows
                if order_room.check_out_date <= order_room.check_in_date
            ),
            None,
        )
        if invalid_range_row is not None:
            order_room, order = invalid_range_row
            add_unresolved(
                "invalid_dates",
                order=order,
                order_room=order_room,
                stay_group_id=stay_group_id,
            )
            return

        room_ids = {order_room.room_id for order_room, _, _ in rows}
        if stay_group_id is not None and len(room_ids) != 1:
            add_unresolved(
                "cross_room_continuation",
                stay_group_id=stay_group_id,
                affected_rows=rows,
            )
            return

        final_checkout = max(order_room.check_out_date for order_room, _, _ in rows)
        final_rows = [row for row in rows if row[0].check_out_date == final_checkout]
        if len(final_rows) != 1:
            add_unresolved(
                "duplicate_final_nodes",
                stay_group_id=stay_group_id,
                affected_rows=final_rows,
            )
            return

        if stay_group_id is not None:
            ordered_rows = sorted(
                rows,
                key=lambda row: (
                    row[0].check_in_date,
                    row[0].check_out_date,
                    row[0].order_room_id,
                ),
            )
            for previous, current in zip(ordered_rows, ordered_rows[1:]):
                previous_checkout = previous[0].check_out_date
                current_checkin = current[0].check_in_date
                if current_checkin < previous_checkout:
                    add_unresolved(
                        "overlapping_stay_group",
                        stay_group_id=stay_group_id,
                        affected_rows=[previous, current],
                    )
                    return
                if current_checkin > previous_checkout:
                    add_unresolved(
                        "gapped_stay_group",
                        stay_group_id=stay_group_id,
                        affected_rows=[previous, current],
                    )
                    return

        final_order_room, final_order, final_room = final_rows[0]
        if final_room.owner_id != owner_id:
            return

        nights = sum(
            (order_room.check_out_date - order_room.check_in_date).days
            for order_room, _, _ in rows
        )
        candidates = (
            (ExpenseCategory.cleaning, fees.checkout_cleaning_fee),
            (ExpenseCategory.laundry, fees.laundry_fee_per_room * final_room.beds),
            (ExpenseCategory.daily_supplies, fees.consumable_fee_per_room_night * nights),
        )
        for category, amount in candidates:
            if amount <= 0:
                continue
            planned = PlannedServiceFee(
                owner_id=owner_id,
                order_id=final_order.order_id,
                room_id=final_room.room_id,
                category=category,
                amount=amount,
                expense_date=final_checkout,
                nights=nights,
                stay_group_id=stay_group_id,
            )
            expected.append(planned)

    for row in ordinary_rows:
        plan_rows([row], None)
    for group_id, rows in grouped_rows.items():
        room_ids = {order_room.room_id for order_room, _, _ in rows}
        group_orders = {order.order_id: order for _, order, _ in rows}.values()
        cross_room_confirmed = all(
            bool(
                (order.metadata_ or {}).get(
                    "service_fee_cross_room_confirmed"
                )
            )
            for order in group_orders
        )
        if (
            None not in room_ids
            and len(room_ids) > 1
            and cross_room_confirmed
        ):
            # A confirmed room move or multi-room booking still has one
            # continuation group, but each physical room creates its own
            # cleaning/laundry boundary and nightly consumables total.
            for room_id in sorted(room_ids):
                plan_rows(
                    [row for row in rows if row[0].room_id == room_id],
                    group_id,
                )
        else:
            plan_rows(rows, group_id)

    keys = {(fee.order_id, fee.room_id) for fee in expected}
    existing_by_key: dict[
        tuple[str, str, ExpenseCategory],
        list[tuple[str, Decimal, date]],
    ] = {}
    if keys:
        existing_rows = (
            await db.execute(
                select(
                    Expense.expense_id,
                    Expense.order_id,
                    Expense.room_id,
                    Expense.category,
                    Expense.amount,
                    Expense.expense_date,
                ).where(
                    or_(
                        Expense.is_service_fee.is_(True),
                        Expense.category == ExpenseCategory.cleaning,
                    ),
                    Expense.is_deleted.is_(False),
                    service_fee_ledger.checkout_service_fee_identity_clause(),
                    tuple_(Expense.order_id, Expense.room_id).in_(keys),
                )
            )
        ).all()
        wrong_month_scopes: set[tuple[str, str]] = set()
        expected_categories = {
            (fee.order_id, fee.room_id, fee.category) for fee in expected
        }
        stay_groups = {
            (fee.order_id, fee.room_id): fee.stay_group_id for fee in expected
        }
        duplicate_keys: set[tuple[str, str, ExpenseCategory]] = set()
        for (
            expense_id,
            existing_order_id,
            existing_room_id,
            category,
            amount,
            expense_date,
        ) in existing_rows:
            key = (existing_order_id, existing_room_id, category)
            scope = (existing_order_id, existing_room_id)
            if (
                key in expected_categories
                and expense_date is not None
                and (expense_date.year, expense_date.month) != (year, month)
                and scope not in wrong_month_scopes
            ):
                wrong_month_scopes.add(scope)
                unresolved.append(
                    UnresolvedServiceFee(
                        reason="wrong_expense_month",
                        order_id=existing_order_id,
                        room_id=existing_room_id,
                        stay_group_id=stay_groups.get(scope),
                    )
                )
            if (
                key in expected_categories
                and expense_date is not None
                and (expense_date.year, expense_date.month) == (year, month)
            ):
                values = existing_by_key.setdefault(key, [])
                values.append((expense_id, amount, expense_date))
                if len(values) > 1 and key not in duplicate_keys:
                    duplicate_keys.add(key)
                    unresolved.append(
                        UnresolvedServiceFee(
                            reason="duplicate_service_fee",
                            order_id=existing_order_id,
                            room_id=existing_room_id,
                            stay_group_id=stay_groups.get(scope),
                        )
                    )

    missing: list[PlannedServiceFee] = []
    corrections: list[PlannedServiceFeeCorrection] = []
    category_totals: dict[ExpenseCategory, Decimal] = {}
    for planned in expected:
        category = planned.category
        amount = planned.amount
        key = (planned.order_id, planned.room_id, category)
        existing = existing_by_key.get(key, [])
        if len(existing) > 1:
            continue
        if len(existing) == 1:
            expense_id, current_amount, current_expense_date = existing[0]
            if current_amount != amount or current_expense_date != planned.expense_date:
                corrections.append(
                    PlannedServiceFeeCorrection(
                        expense_id=expense_id,
                        current_amount=current_amount,
                        current_expense_date=current_expense_date,
                        expected=planned,
                    )
                )
                category_totals[category] = category_totals.get(
                    category, Decimal("0")
                ) + amount
            continue
        missing.append(planned)
        category_totals[category] = category_totals.get(category, Decimal("0")) + amount

    return ServiceFeeReconciliationPlan(
        owner_id=owner_id,
        year=year,
        month=month,
        expected=tuple(expected),
        missing=tuple(missing),
        corrections=tuple(corrections),
        unresolved=tuple(unresolved),
        category_totals=MappingProxyType(category_totals),
    )


_REPAIR_DESCRIPTIONS = {
    ExpenseCategory.cleaning: "退房打扫（结算前对账补录）",
    ExpenseCategory.laundry: "洗涤费（结算前对账补录）",
    ExpenseCategory.daily_supplies: "日耗品（结算前对账补录）",
}


def _money(value: Decimal) -> str:
    return f"{value:.2f}"


async def apply_service_fee_reconciliation(
    db: AsyncSession,
    plan: ServiceFeeReconciliationPlan,
    operator_id: str | None,
) -> ServiceFeeRepairResult:
    """Add deterministic missing fees and their audit row to the caller transaction.

    Applying a plan with ambiguity is forbidden.  The owner row is locked and
    active service-fee keys are re-read because the read-only plan can be stale by
    the time its caller applies it.  This function flushes for immediate visibility
    but deliberately leaves commit or rollback to the settlement transaction.
    """
    if plan.unresolved:
        raise ServiceFeeReconciliationError(plan)

    empty_result = ServiceFeeRepairResult(
        created_count=0,
        corrected_count=0,
        created_amount=Decimal("0"),
        category_totals=MappingProxyType({}),
    )
    if not plan.missing and not plan.corrections:
        return empty_result

    # Serialise reconciliation writers for one owner.  Unlike locking missing
    # Expense rows, this stable row also protects the empty-key (first writer) case.
    await service_fee_ledger.lock_owner_service_fee_ledger(db, plan.owner_id)

    correction_by_id = {item.expense_id: item for item in plan.corrections}
    planned_fees = list(plan.missing) + [item.expected for item in plan.corrections]
    pairs = {(fee.order_id, fee.room_id) for fee in planned_fees}
    existing_rows = (
        await db.execute(
            select(Expense).where(
                or_(
                    Expense.is_service_fee.is_(True),
                    Expense.category == ExpenseCategory.cleaning,
                ),
                Expense.is_deleted.is_(False),
                service_fee_ledger.checkout_service_fee_identity_clause(),
                tuple_(Expense.order_id, Expense.room_id).in_(pairs),
            ).with_for_update()
        )
    ).scalars().all()
    wrong_month_scopes: set[tuple[str, str]] = set()
    for row in existing_rows:
        if (
            row.expense_date is not None
            and (row.expense_date.year, row.expense_date.month) != (plan.year, plan.month)
            and any(
                fee.order_id == row.order_id
                and fee.room_id == row.room_id
                and fee.category == row.category
                for fee in planned_fees
            )
        ):
            wrong_month_scopes.add((row.order_id, row.room_id))
    if wrong_month_scopes:
        drift = tuple(
            UnresolvedServiceFee(
                reason="wrong_expense_month",
                order_id=order_id,
                room_id=room_id,
            )
            for order_id, room_id in sorted(wrong_month_scopes)
        )
        raise ServiceFeeReconciliationError(
            replace(plan, unresolved=plan.unresolved + drift)
        )
    active_by_key: dict[tuple[str, str, ExpenseCategory], list[Expense]] = {}
    for row in existing_rows:
        if row.expense_date is None or (
            row.expense_date.year, row.expense_date.month
        ) != (plan.year, plan.month):
            continue
        active_by_key.setdefault(
            (row.order_id, row.room_id, row.category), []
        ).append(row)

    drift: list[UnresolvedServiceFee] = []
    corrections_to_apply: list[PlannedServiceFeeCorrection] = []
    for correction in plan.corrections:
        row = next(
            (item for item in existing_rows if item.expense_id == correction.expense_id),
            None,
        )
        key = (
            correction.expected.order_id,
            correction.expected.room_id,
            correction.expected.category,
        )
        active_rows = active_by_key.get(key, [])
        if (
            row is None
            and len(active_rows) == 1
            and active_rows[0].amount == correction.expected.amount
            and active_rows[0].expense_date == correction.expected.expense_date
        ):
            # A retry of the same stale plan sees the replacement row and is a no-op.
            continue
        if (
            row is None
            or len(active_rows) != 1
            or row.amount != correction.current_amount
            or row.expense_date != correction.current_expense_date
        ):
            drift.append(
                UnresolvedServiceFee(
                    reason="service_fee_changed_during_repair",
                    order_id=correction.expected.order_id,
                    room_id=correction.expected.room_id,
                    stay_group_id=correction.expected.stay_group_id,
                )
            )
            continue
        corrections_to_apply.append(correction)
    if drift:
        raise ServiceFeeReconciliationError(
            replace(plan, unresolved=plan.unresolved + tuple(drift))
        )

    now = datetime.now(timezone.utc)
    for correction in corrections_to_apply:
        row = next(
            item
            for item in existing_rows
            if item.expense_id == correction.expense_id
        )
        row.is_deleted = True
        row.deleted_at = now
        row.deleted_by = operator_id
        row.notes = (
            (row.notes + "；") if row.notes else ""
        ) + "结算前服务费对账：金额或日期与最终订单不一致，已自动更正"

    existing_keys = {
        key
        for key, rows in active_by_key.items()
        if len(rows) == 1 and rows[0].expense_id not in correction_by_id
    }

    created: list[PlannedServiceFee] = []
    seen_keys = set(existing_keys)
    for fee in planned_fees:
        key = (fee.order_id, fee.room_id, fee.category)
        if key in seen_keys:
            continue
        seen_keys.add(key)
        stay_group_note = fee.stay_group_id or "-"
        db.add(
            Expense(
                expense_id="EXP-" + uuid4().hex[:12].upper(),
                category=fee.category,
                amount=fee.amount,
                description=_REPAIR_DESCRIPTIONS.get(
                    fee.category, "结算前对账补录：服务费"
                ),
                expense_date=fee.expense_date,
                room_id=fee.room_id,
                order_id=fee.order_id,
                payer=ExpensePayer.owner,
                owner_id=fee.owner_id,
                notes=(
                    "结算前服务费对账自动补录；"
                    f"owner={plan.owner_id}；month={plan.year:04d}-{plan.month:02d}；"
                    f"stay_group={stay_group_note}"
                ),
                created_by=operator_id,
                is_service_fee=True,
            )
        )
        created.append(fee)

    if not created:
        return empty_result

    category_totals: dict[ExpenseCategory, Decimal] = {}
    for fee in created:
        category_totals[fee.category] = (
            category_totals.get(fee.category, Decimal("0")) + fee.amount
        )
    created_amount = sum(category_totals.values(), Decimal("0"))
    await log_action_tx(
        db,
        operator_id,
        "settlement.service_fee_repair",
        "owner",
        plan.owner_id,
        after_data={
            "status": "auto_repaired",
            "owner_id": plan.owner_id,
            "year": plan.year,
            "month": plan.month,
            "created_count": len(created),
            "corrected_count": len(corrections_to_apply),
            "created_amount": _money(created_amount),
            "category_totals": {
                category.value: _money(amount)
                for category, amount in category_totals.items()
            },
        },
        notes=(
            "结算前服务费对账自动补账 "
            f"{plan.year:04d}-{plan.month:02d}，共 {len(created)} 笔"
        ),
    )
    await db.flush()
    return ServiceFeeRepairResult(
        created_count=len(created),
        corrected_count=len(corrections_to_apply),
        created_amount=created_amount,
        category_totals=MappingProxyType(category_totals),
    )
