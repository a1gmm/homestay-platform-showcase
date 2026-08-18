"""Canonical, current-state manual ownership fields for order synchronisation."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from datetime import date, datetime, timezone
from decimal import Decimal
from enum import Enum, StrEnum
import hashlib
import json
from typing import TYPE_CHECKING, Iterable, Literal, Mapping
from uuid import uuid4

from sqlalchemy import and_, exists, or_, select

from app.core.config import settings

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.models.order import Order
    from app.models.order_operation import OrderOperation
    from app.models.order_sync_conflict import OrderSyncConflict


MANUAL_OVERRIDE_VERSION = 1


class OrderSyncField(StrEnum):
    guest_name = "guest_name"
    guest_profile = "guest_profile"
    check_in_date = "check_in_date"
    check_out_date = "check_out_date"
    room_assignment = "room_assignment"
    stay_structure = "stay_structure"
    actual_price = "actual_price"
    daily_prices = "daily_prices"
    ota_owner_revenue = "ota_owner_revenue"
    channel = "channel"
    note = "note"
    order_status = "order_status"


SNAPSHOT_LOCK_FIELDS: dict[str, frozenset[str]] = {
    "guest_name": frozenset({"guest_name"}),
    "guest_phone": frozenset({"guest_profile"}),
    "check_in_date": frozenset({"check_in_date"}),
    "check_out_date": frozenset({"check_out_date"}),
    "room_id": frozenset({"room_assignment"}),
    "rooms": frozenset({
        "room_assignment", "stay_structure", "check_in_date", "check_out_date",
        "actual_price", "daily_prices",
    }),
    "actual_price": frozenset({"actual_price", "daily_prices"}),
    "ota_owner_revenue": frozenset({"ota_owner_revenue"}),
    "channel": frozenset({"channel"}),
    "note": frozenset({"note"}),
    "status": frozenset({"order_status"}),
    "stay_group_id": frozenset({"stay_structure"}),
}

# Every authenticated, state-changing order endpoint is declared here, including
# surfaces that do not write an upstream-owned field (empty set). Tests discover
# registered POST/PATCH/DELETE routes and compare them to this complete inventory,
# so adding a route cannot silently skip the ownership decision.
HUMAN_MUTATION_ROUTE_FIELDS: dict[tuple[str, str], frozenset[str]] = {
    ("POST", "/orders"): frozenset(),
    ("POST", "/orders/{order_id}/zero-fee-split/preview"): frozenset(),
    ("POST", "/orders/{order_id}/zero-fee-split"): frozenset({
        "stay_structure", "check_in_date", "check_out_date", "room_assignment",
        "actual_price", "daily_prices",
    }),
    ("POST", "/orders/{order_id}/source-price-snapshots/admin-override"): frozenset(),
    ("POST", "/orders/{order_id}/sync-conflicts/{conflict_id}/decision"): frozenset(),
    ("POST", "/orders/{order_id}/sponsorship-corrections"): frozenset(),
    ("PATCH", "/orders/{order_id}"): frozenset({
        "guest_name", "guest_profile", "check_in_date", "check_out_date",
        "room_assignment", "stay_structure", "actual_price", "daily_prices",
        "ota_owner_revenue", "channel", "note",
    }),
    ("PATCH", "/orders/{order_id}/manual-overrides"): frozenset(),
    ("PATCH", "/orders/{order_id}/rooms/{order_room_id}/daily-price"): frozenset({
        "actual_price", "daily_prices",
    }),
    ("POST", "/orders/{order_id}/cancel"): frozenset({"order_status"}),
    ("DELETE", "/orders/{order_id}"): frozenset({"order_status"}),
    ("POST", "/orders/batch/transition"): frozenset({"order_status"}),
    ("POST", "/orders/{order_id}/transition"): frozenset({"order_status"}),
    ("POST", "/orders/{order_id}/lock/extend-code"): frozenset(),
    ("POST", "/orders/{order_id}/lock/resend-card"): frozenset(),
    ("POST", "/orders/{order_id}/link-continuation"): frozenset({"stay_structure"}),
    ("POST", "/orders/{order_id}/unlink-continuation"): frozenset({"stay_structure"}),
    ("POST", "/orders/{order_id}/assign-room"): frozenset({
        "room_assignment", "order_status",
    }),
    ("POST", "/orders/{order_id}/transfer-room"): frozenset({
        "room_assignment", "stay_structure", "check_in_date", "check_out_date",
        "actual_price", "daily_prices",
    }),
    ("POST", "/orders/swap-rooms"): frozenset({
        "room_assignment", "stay_structure", "check_in_date", "check_out_date",
        "actual_price", "daily_prices",
    }),
    ("POST", "/orders/{order_id}/deposit/collect"): frozenset(),
    ("POST", "/orders/{order_id}/deposit/return"): frozenset(),
    ("POST", "/orders/{order_id}/deposit/withhold"): frozenset(),
    ("POST", "/staff/orders/{order_id}/handle-checkin"): frozenset({
        "order_status", "note",
    }),
    ("POST", "/staff/orders/{order_id}/handle-checkout"): frozenset({
        "order_status", "note",
    }),
    ("POST", "/staff/orders/{order_id}/revert-checkout"): frozenset({"order_status"}),
    ("POST", "/staff/orders/{order_id}/revert-checkin"): frozenset({"order_status"}),
}


LEGACY_LOCKS: dict[str, frozenset[str]] = {
    "price_locked": frozenset({"actual_price", "daily_prices", "ota_owner_revenue"}),
    "room_locked": frozenset({"room_assignment"}),
}

_KNOWN_FIELDS = frozenset(field.value for field in OrderSyncField)


class IdempotencyKeyReusedError(RuntimeError):
    """A key is bound to a different normalized request (map to HTTP 409)."""

    code = "IDEMPOTENCY_KEY_REUSED"


class OperationInProgressError(RuntimeError):
    """A matching request is still executing and cannot safely be replayed."""

    code = "OPERATION_IN_PROGRESS"


@dataclass(frozen=True)
class OrderOperationClaim:
    operation: "OrderOperation"
    is_replay: bool


def _is_legacy_lock_enabled(value: object) -> bool:
    return value is True or (isinstance(value, str) and value.lower() == "true")


def _normalise_fields(fields: Iterable[str | OrderSyncField], *, strict: bool) -> frozenset[str]:
    normalised = frozenset(
        field.value if isinstance(field, OrderSyncField) else str(field)
        for field in fields
    )
    unknown = normalised - _KNOWN_FIELDS
    if strict and unknown:
        raise ValueError(f"Unsupported order sync field(s): {', '.join(sorted(unknown))}")
    return normalised - unknown


def locked_fields(metadata: Mapping[str, object] | None) -> frozenset[str]:
    """Return canonical locks, including the two legacy metadata flags.

    Metadata remains a compact current-state projection.  Callers that persist a
    changed value must assign the returned copy instead of mutating JSON in place.
    """
    data = metadata or {}
    fields: set[str] = set()
    override = data.get("manual_override")
    if isinstance(override, Mapping):
        stored = override.get("fields", ())
        if isinstance(stored, (list, tuple, set, frozenset)):
            fields.update(_normalise_fields(stored, strict=False))

    for legacy_key, legacy_fields in LEGACY_LOCKS.items():
        if _is_legacy_lock_enabled(data.get(legacy_key)):
            fields.update(legacy_fields)
    return frozenset(fields)


def lock_fields(
    metadata: Mapping[str, object] | None,
    fields: Iterable[str | OrderSyncField],
) -> dict:
    """Return a copied metadata value with the supplied fields canonically locked."""
    out = deepcopy(dict(metadata or {}))
    combined = locked_fields(out) | _normalise_fields(fields, strict=True)
    out["manual_override"] = {
        "version": MANUAL_OVERRIDE_VERSION,
        "fields": sorted(combined),
    }
    return out


def _snapshot_lock_value(snapshot: Mapping[str, object], field: str) -> object:
    value = snapshot.get(field)
    if field != "rooms" or not isinstance(value, list):
        return value
    # OrderUpdate replaces child rows, so generated order_room_id values always
    # change. Compare only upstream-owned room facts, never persistence IDs or
    # local-only guest counts.
    return [
        {
            key: room.get(key)
            for key in ("room_id", "check_in_date", "check_out_date", "actual_price")
        }
        for room in value
        if isinstance(room, Mapping)
    ]


def changed_snapshot_sync_fields(
    before: Mapping[str, object],
    after: Mapping[str, object],
) -> frozenset[str]:
    """Map real business changes in order snapshots to canonical sync fields."""
    return frozenset(
        sync_field
        for snapshot_field, sync_fields in SNAPSHOT_LOCK_FIELDS.items()
        if _snapshot_lock_value(before, snapshot_field)
        != _snapshot_lock_value(after, snapshot_field)
        for sync_field in sync_fields
    )


def apply_snapshot_manual_locks(
    order: "Order",
    before: Mapping[str, object],
    after: Mapping[str, object],
    *,
    source: Literal["human", "system"],
) -> frozenset[str]:
    """Lock sync-owned fields changed by an explicitly human mutation."""
    if source != "human":
        return frozenset()
    changed = changed_snapshot_sync_fields(before, after)
    if changed and settings.PMS_CANONICAL_LOCK_WRITE_ENABLED:
        order.metadata_ = lock_fields(order.metadata_, changed)
    return changed


def unlock_fields(
    metadata: Mapping[str, object] | None,
    fields: Iterable[str | OrderSyncField],
) -> dict:
    """Return a copied metadata value with only the selected fields unlocked.

    Releasing any field represented by a legacy flag clears that flag and retains
    the other formerly-legacy fields in the canonical set, so the selected field
    genuinely resumes upstream ownership during the compatibility period.
    """
    out = deepcopy(dict(metadata or {}))
    selected = _normalise_fields(fields, strict=True)
    remaining = locked_fields(out) - selected
    for legacy_key, legacy_fields in LEGACY_LOCKS.items():
        if selected & legacy_fields:
            out.pop(legacy_key, None)
    out["manual_override"] = {
        "version": MANUAL_OVERRIDE_VERSION,
        "fields": sorted(remaining),
    }
    return out


def is_field_locked(
    metadata: Mapping[str, object] | None,
    field: str | OrderSyncField,
) -> bool:
    value = field.value if isinstance(field, OrderSyncField) else str(field)
    return value in locked_fields(metadata)


def _json_safe(value: object) -> object:
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        # JSON numbers are acceptable here, but reject NaN/Infinity deterministically.
        json.dumps(value, allow_nan=False)
        return value
    if isinstance(value, Decimal):
        return format(value, "f")
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Enum):
        return _json_safe(value.value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        normalised = [_json_safe(item) for item in value]
        return sorted(normalised, key=_canonical_json)
    raise TypeError(f"Value is not JSON-safe: {type(value).__name__}")


def _canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _json_value(value: object) -> object:
    """Return a JSONB-safe value with canonical mappings, dates, enums, and money."""
    return json.loads(_canonical_json(_json_safe(value)))


def _insert_do_nothing(db: "AsyncSession", model, values: dict, *, index_elements: list[str]):
    """Build the native conflict-safe INSERT supported by production and test DBs."""
    dialect_name = db.get_bind().dialect.name
    if dialect_name == "postgresql":
        from sqlalchemy.dialects.postgresql import insert
    elif dialect_name == "sqlite":
        from sqlalchemy.dialects.sqlite import insert
    else:  # The application supports PostgreSQL; SQLite is the fast test harness.
        raise RuntimeError(f"Unsupported idempotency dialect: {dialect_name}")
    return insert(model).values(**values).on_conflict_do_nothing(
        index_elements=index_elements
    )


def normalized_request_hash(payload: Mapping[str, object]) -> str:
    """SHA-256 of a deterministic, JSON-safe request representation."""
    encoded = _canonical_json(_json_safe(payload)).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


async def record_order_sync_conflict(
    db: "AsyncSession",
    *,
    source_order_id: str,
    field: str,
    local_value: object,
    upstream_value: object,
    upstream_version: str,
    conflict_id: str | None = None,
) -> "OrderSyncConflict | None":
    """Upsert a blocked value or automatically resolve open values that converge."""
    from app.models.order_sync_conflict import OrderSyncConflict, OrderSyncConflictStatus

    local = _json_value(local_value)
    upstream = _json_value(upstream_value)
    now = datetime.now(timezone.utc)
    if local == upstream:
        open_rows = (await db.execute(
            select(OrderSyncConflict).where(
                OrderSyncConflict.source_order_id == source_order_id,
                OrderSyncConflict.field == field,
                OrderSyncConflict.status == OrderSyncConflictStatus.open,
            )
        )).scalars().all()
        for row in open_rows:
            row.status = OrderSyncConflictStatus.resolved
            row.last_seen_at = now
            row.resolved_at = now
        await db.flush()
        return None

    row = (await db.execute(
        select(OrderSyncConflict).where(
            OrderSyncConflict.source_order_id == source_order_id,
            OrderSyncConflict.field == field,
            OrderSyncConflict.upstream_version == upstream_version,
        )
    )).scalar_one_or_none()
    if row is None:
        inserted_id = (await db.execute(
            _insert_do_nothing(
                db,
                OrderSyncConflict,
                {
                    "conflict_id": conflict_id or f"SC-{uuid4().hex[:20].upper()}",
                    "source_order_id": source_order_id,
                    "field": field,
                    "local_value": local,
                    "upstream_value": upstream,
                    "upstream_version": upstream_version,
                    "status": OrderSyncConflictStatus.open,
                    "first_seen_at": now,
                    "last_seen_at": now,
                },
                index_elements=["source_order_id", "field", "upstream_version"],
            ).returning(OrderSyncConflict.conflict_id)
        )).scalar_one_or_none()
        if inserted_id is not None:
            row = await db.get(OrderSyncConflict, inserted_id)
        else:
            # A concurrent PostgreSQL INSERT won the unique key.  At READ
            # COMMITTED this follow-up statement sees it after ON CONFLICT waits.
            row = (await db.execute(
                select(OrderSyncConflict)
                .where(
                    OrderSyncConflict.source_order_id == source_order_id,
                    OrderSyncConflict.field == field,
                    OrderSyncConflict.upstream_version == upstream_version,
                )
                .with_for_update()
            )).scalar_one()
    # Apply the same latest-observation refresh after every non-equal path,
    # including a row just reloaded after another transaction won the INSERT.
    row.local_value = local
    row.upstream_value = upstream
    row.last_seen_at = now
    if row.status is OrderSyncConflictStatus.resolved:
        row.status = OrderSyncConflictStatus.open
        row.resolved_at = None
        row.resolved_by = None
        row.resolved_audit_log_id = None
    await db.flush()
    return row


async def list_order_sync_conflicts(
    db: "AsyncSession", *, source_order_id: str, property_scope: str
) -> list["OrderSyncConflict"]:
    """Return conflicts only when the source order belongs to the requested owner scope."""
    from app.models.order import Order
    from app.models.order_room import OrderRoom
    from app.models.order_sync_conflict import OrderSyncConflict
    from app.models.room import Room

    has_order_rooms = exists(
        select(OrderRoom.order_room_id).where(OrderRoom.order_id == Order.order_id)
    )
    scoped_order_room = exists(
        select(OrderRoom.order_room_id)
        .join(Room, Room.room_id == OrderRoom.room_id)
        .where(OrderRoom.order_id == Order.order_id, Room.owner_id == property_scope)
    )
    legacy_scoped_room = exists(
        select(Room.room_id).where(Room.room_id == Order.room_id, Room.owner_id == property_scope)
    )
    rows = await db.execute(
        select(OrderSyncConflict)
        .join(Order, Order.order_id == OrderSyncConflict.source_order_id)
        .where(
            OrderSyncConflict.source_order_id == source_order_id,
            or_(
                scoped_order_room,
                and_(~has_order_rooms, legacy_scoped_room),
            ),
        )
        .order_by(OrderSyncConflict.first_seen_at, OrderSyncConflict.conflict_id)
    )
    return list(rows.scalars().all())


async def claim_order_operation(
    db: "AsyncSession",
    *,
    property_scope: str,
    operation: str,
    idempotency_key: str,
    request_payload: Mapping[str, object],
    operation_id: str | None = None,
) -> OrderOperationClaim:
    """Claim a persistent operation key, replay it, or safely retry a failed claim."""
    from app.models.order_operation import OrderOperation, OrderOperationStatus

    request_hash = normalized_request_hash(request_payload)
    existing = (await db.execute(
        select(OrderOperation)
        .where(
            OrderOperation.property_scope == property_scope,
            OrderOperation.operation == operation,
            OrderOperation.idempotency_key == idempotency_key,
        )
        .with_for_update()
    )).scalar_one_or_none()
    if existing is None:
        inserted_id = (await db.execute(
            _insert_do_nothing(
                db,
                OrderOperation,
                {
                    "operation_id": operation_id or f"OP-{uuid4().hex[:20].upper()}",
                    "property_scope": property_scope,
                    "operation": operation,
                    "idempotency_key": idempotency_key,
                    "request_hash": request_hash,
                    "status": OrderOperationStatus.in_progress,
                    "result_order_ids": [],
                },
                index_elements=["property_scope", "operation", "idempotency_key"],
            ).returning(OrderOperation.operation_id)
        )).scalar_one_or_none()
        if inserted_id is not None:
            created = await db.get(OrderOperation, inserted_id)
            return OrderOperationClaim(operation=created, is_replay=False)
        # A concurrent first claimant won.  ON CONFLICT has waited for its
        # outcome, so reloading produces the regular replay/collision result.
        existing = (await db.execute(
            select(OrderOperation)
            .where(
                OrderOperation.property_scope == property_scope,
                OrderOperation.operation == operation,
                OrderOperation.idempotency_key == idempotency_key,
            )
            .with_for_update()
        )).scalar_one()

    if existing.request_hash != request_hash:
        raise IdempotencyKeyReusedError("Idempotency key is already bound to a different request")
    if existing.status is OrderOperationStatus.succeeded:
        return OrderOperationClaim(operation=existing, is_replay=True)
    if existing.status is OrderOperationStatus.in_progress:
        raise OperationInProgressError("Operation with this idempotency key is in progress")

    existing.status = OrderOperationStatus.in_progress
    existing.result_order_ids = []
    existing.result_stay_group_id = None
    existing.error_code = None
    await db.flush()
    return OrderOperationClaim(operation=existing, is_replay=False)


async def complete_order_operation(
    db: "AsyncSession",
    operation: "OrderOperation",
    *,
    result_order_ids: Iterable[str],
    result_stay_group_id: str | None,
) -> None:
    from app.models.order_operation import OrderOperationStatus

    operation.status = OrderOperationStatus.succeeded
    operation.result_order_ids = list(result_order_ids)
    operation.result_stay_group_id = result_stay_group_id
    operation.error_code = None
    await db.flush()


async def mark_order_operation_failed(
    db: "AsyncSession", operation: "OrderOperation", *, error_code: str
) -> None:
    from app.models.order_operation import OrderOperationStatus

    operation.status = OrderOperationStatus.failed
    operation.error_code = error_code
    await db.flush()
