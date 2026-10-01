"""Focused, PII-free read model for shared ota-sync journal tables."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import math
import re
import secrets
import sqlite3
from collections.abc import Mapping
from datetime import date, datetime, timedelta, timezone
from typing import Any, get_args

from sqlalchemy import (
    JSON,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    and_,
    case,
    func,
    insert,
    or_,
    select,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.order import Channel, Order, OrderStatus
from app.models.order_sync_conflict import OrderSyncConflict, OrderSyncConflictStatus
from app.schemas.bypms_sync import (
    BypmsSyncConflict,
    BypmsSyncConflictsResponse,
    BypmsSyncCycle,
    BypmsSyncCyclesResponse,
    BypmsSyncCycleWithSteps,
    BypmsSyncOverview,
    BypmsSyncRetryResponse,
    BypmsSyncStep,
    BypmsSyncStepSummary,
    ConflictStatus,
    RetryActor,
    RetryStatus,
)
from app.services.audit import log_action_tx
from app.services.manual_override import OrderSyncField


logger = logging.getLogger(__name__)

ROLLOUT_MESSAGE = "等待同步服务升级"
MAX_JOURNAL_JSON_BYTES = 16 * 1024
MAX_JSON_DEPTH = 8
MAX_JSON_ITEMS = 256
MAX_JSON_STRING_LENGTH = 1024
CLOCK_SKEW_TOLERANCE_SECONDS = 60

_CYCLE_STATUSES = frozenset({"running", "succeeded", "partial", "failed", "skipped"})
_TRIGGERS = frozenset({"scheduled", "admin_retry"})
_STEP_NAMES = frozenset({
    "pull",
    "price_reconcile",
    "name_reconcile",
    "create",
    "date_reconcile",
    "room_reconcile",
    "assign",
    "status_reconcile",
    "subsidized_scan",
    "cancel",
    "room_status_reconcile",
})
_STEP_STATUSES = frozenset({"running", "succeeded", "failed", "skipped"})
_SUMMARY_STATUSES = frozenset({"success", "failure", "skipped"})
_WRITE_MODE_KEYS = frozenset({
    "date_reconcile",
    "room_reconcile",
    "status_reconcile",
    "room_status_reconcile",
})
_SAFE_COUNT_FIELDS = frozenset({
    "active",
    "adopted",
    "alert_deposit",
    "alert_manual",
    "alerted",
    "assigned",
    "cancelled",
    "changed",
    "completed",
    "conflicts",
    "created",
    "excluded",
    "fetched",
    "fixed",
    "found",
    "incremented",
    "multi_room",
    "noop",
    "reset",
    "restructure",
    "scanned",
    "seen",
    "skipped",
    "skipped_ambiguous",
    "skipped_locked",
    "subsidy_written",
    "to_adopt",
    "to_complete",
    "to_create",
    "to_fix",
    "total",
    "total_fetched",
    "unmapped",
    "written",
})
_SAFE_ERROR_CODES = frozenset({
    "authentication_failed",
    "cancelled",
    "configuration_invalid",
    "conflict",
    "database_unavailable",
    "internal_error",
    "incomplete_response",
    "network_error",
    "not_found",
    "timeout",
    "unknown",
    "upstream_unavailable",
    "upstream_http_error",
    "upstream_response_invalid",
    "validation_failed",
})
_KNOWN_CONFLICT_FIELDS = frozenset(field.value for field in OrderSyncField)
_CYCLE_ID_RE = re.compile(r"^CYC-[0-9a-f]{20}$")
_REQUEST_ID_RE = re.compile(r"^REQ-[0-9a-f]{20}$")
_CONFLICT_ID_RE = re.compile(r"^SC-[0-9A-F]{20}$")
_ORDER_ID_RE = re.compile(r"^ORD-\d{8}-C?(?:[0-9A-F]{4}|[0-9A-F]{6})$")
_ORDER_ROOM_ID_RE = re.compile(r"^OR-(?:[0-9A-F]{16}|[0-9A-F]{20})$")
_ROOM_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,9}$")
_STAY_GROUP_ID_RE = re.compile(r"^sg_[0-9a-f]{12}$")
_PHONE_LIKE_RE = re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)")
_MONEY_RE = re.compile(r"^(?:0|[1-9]\d{0,7})(?:\.\d{1,2})?$")
_SENSITIVE_KEY_PARTS = frozenset({
    "authorization",
    "cookie",
    "credential",
    "detail_payload",
    "guest",
    "id_number",
    "password",
    "phone",
    "raw_payload",
    "secret",
    "session",
    "token",
})
_REDACTED = "[REDACTED]"
_SENSITIVE_CONFLICT_FIELDS = frozenset({"guest_name", "guest_profile", "note"})
_SAFE_CHANNELS = frozenset(channel.value for channel in Channel)
_SAFE_ORDER_STATUSES = frozenset(status.value for status in OrderStatus)
_CONFLICT_STATUSES = frozenset(get_args(ConflictStatus))
_SAFE_REQUEST_ACTORS = frozenset(get_args(RetryActor))
_SAFE_REQUEST_STATUSES = frozenset(get_args(RetryStatus))
_IDEMPOTENCY_DOMAIN = b"bypms-sync-request-idempotency:v1\0"
_HMAC_SCHEME = "hmac-sha256:v1"
_LEGACY_SCHEME = "sha256:v1"
_RETRY_ADVISORY_LOCK_KEY = 0x4259504D53525931
_INVALID_CONFLICT_VALUE = object()


_json_type = JSON().with_variant(JSONB(), "postgresql")
integration_read_metadata = MetaData()

bypms_sync_cycles = Table(
    "bypms_sync_cycles",
    integration_read_metadata,
    Column("cycle_id", String(24), primary_key=True),
    Column("trigger", String(32), nullable=False),
    Column("write_modes", _json_type, nullable=False),
    Column("status", String(20), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
)
bypms_sync_steps = Table(
    "bypms_sync_steps",
    integration_read_metadata,
    Column("step_id", Integer, primary_key=True),
    Column("cycle_id", String(24), nullable=False),
    Column("name", String(64), nullable=False),
    Column("status", String(20), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("finished_at", DateTime(timezone=True)),
    Column("summary", _json_type),
)
bypms_sync_requests = Table(
    "bypms_sync_requests",
    integration_read_metadata,
    Column("request_id", String(24), primary_key=True),
    Column("requested_by", String(255), nullable=False),
    Column("idempotency_key", String(255), nullable=False),
    Column("idempotency_scheme", String(32), nullable=False),
    Column("status", String(20), nullable=False),
    Column("cycle_id", String(24)),
    Column("requested_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("finished_at", DateTime(timezone=True)),
)
ota_raw_orders = Table(
    "ota_raw_orders",
    integration_read_metadata,
    Column("platform", String(20), nullable=False),
    Column("fetched_at", DateTime(timezone=True), nullable=False),
)


class IntegrationTablesUnavailable(RuntimeError):
    """The ota-sync hardening schema has not landed yet."""


class RetryUnavailableError(RuntimeError):
    """Retry cannot safely mutate the shared queue in the current rollout state."""


class RetryAlreadyActiveError(RuntimeError):
    """A different trusted retry is already queued or running."""


class RetryRequestFailedError(RuntimeError):
    """An unexpected database failure prevented a safe retry request."""


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _duration_ms(
    started_at: datetime,
    finished_at: datetime | None,
    now: datetime,
) -> int:
    start = _as_utc(started_at)
    end = _as_utc(finished_at) or _as_utc(now)
    assert start is not None and end is not None
    return max(0, int((end - start).total_seconds() * 1000))


def _is_future_beyond_clock_skew_tolerance(
    value: datetime | None,
    now: datetime,
) -> bool:
    timestamp = _as_utc(value)
    reference = _as_utc(now)
    assert reference is not None
    return bool(
        timestamp is not None
        and timestamp
        > reference + timedelta(seconds=CLOCK_SKEW_TOLERANCE_SECONDS)
    )


def _age_seconds_or_none(value: datetime | None, now: datetime) -> int | None:
    timestamp = _as_utc(value)
    reference = _as_utc(now)
    assert reference is not None
    if timestamp is None or _is_future_beyond_clock_skew_tolerance(
        timestamp, reference
    ):
        return None
    return max(0, int((reference - timestamp).total_seconds()))


def _has_sensitive_key(key: str) -> bool:
    normalized = key.strip().lower().replace("-", "_")
    return any(part in normalized for part in _SENSITIVE_KEY_PARTS)


def _fits_journal_json_byte_limit(value: Any) -> bool:
    """Measure deterministic final JSON bytes after field-level validation."""
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError, OverflowError):
        return False
    return len(encoded) <= MAX_JOURNAL_JSON_BYTES


def _bounded_json(value: Any, *, depth: int = 0) -> Any | None:
    """Return bounded JSON primitives, rejecting sensitive or oversized content."""
    if depth > MAX_JSON_DEPTH:
        return None
    if value is None or isinstance(value, bool):
        safe: Any = value
    elif isinstance(value, int):
        safe = value if -(2**63) <= value <= (2**63 - 1) else None
    elif isinstance(value, float):
        safe = value if math.isfinite(value) else None
    elif isinstance(value, str):
        safe = value if len(value) <= MAX_JSON_STRING_LENGTH else None
    elif isinstance(value, (list, tuple)):
        if len(value) > MAX_JSON_ITEMS:
            return None
        safe = []
        for item in value:
            bounded = _bounded_json(item, depth=depth + 1)
            if bounded is None and item is not None:
                return None
            safe.append(bounded)
    elif isinstance(value, Mapping):
        if len(value) > MAX_JSON_ITEMS:
            return None
        safe = {}
        for raw_key, item in value.items():
            if not isinstance(raw_key, str):
                return None
            if len(raw_key) > 128 or _has_sensitive_key(raw_key):
                continue
            bounded = _bounded_json(item, depth=depth + 1)
            if bounded is None and item is not None:
                return None
            safe[raw_key] = bounded
    else:
        return None
    return safe if _fits_journal_json_byte_limit(safe) else None


def _safe_write_modes(value: Any) -> dict[str, bool]:
    bounded = _bounded_json(value)
    if not isinstance(bounded, dict):
        return {}
    return {
        key: mode
        for key, mode in bounded.items()
        if key in _WRITE_MODE_KEYS and isinstance(mode, bool)
    }


def _safe_trigger(value: Any) -> str:
    return value if isinstance(value, str) and value in _TRIGGERS else "unknown"


def _safe_step_name(value: Any) -> str:
    return value if isinstance(value, str) and value in _STEP_NAMES else "unknown"


def _safe_cycle_id(value: Any) -> str:
    return value if isinstance(value, str) and _CYCLE_ID_RE.fullmatch(value) else "unknown"


def _safe_channel(value: Any) -> str:
    raw = value.value if isinstance(value, Channel) else value
    return raw if isinstance(raw, str) and raw in _SAFE_CHANNELS else "unknown"


def _safe_iso_date(value: Any) -> str | None | object:
    if value is None:
        return None
    if not isinstance(value, str):
        return _INVALID_CONFLICT_VALUE
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        return _INVALID_CONFLICT_VALUE
    return value if parsed.isoformat() == value else _INVALID_CONFLICT_VALUE


def _safe_money(value: Any) -> str | None | object:
    if value is None:
        return None
    return (
        value
        if isinstance(value, str) and _MONEY_RE.fullmatch(value)
        else _INVALID_CONFLICT_VALUE
    )


def _safe_room_id(value: Any) -> str | None | object:
    if value is None:
        return None
    return (
        value
        if isinstance(value, str) and _ROOM_ID_RE.fullmatch(value)
        else _INVALID_CONFLICT_VALUE
    )


def _safe_room_assignment(value: Any) -> list[str] | object:
    if not isinstance(value, list) or len(value) > 64:
        return _INVALID_CONFLICT_VALUE
    safe_room_ids: list[str] = []
    for item in value:
        safe = _safe_room_id(item)
        if not isinstance(safe, str):
            return _INVALID_CONFLICT_VALUE
        safe_room_ids.append(safe)
    return safe_room_ids


def _safe_daily_price_values(value: Any) -> dict[str, str] | object:
    if not isinstance(value, Mapping) or len(value) > MAX_JSON_ITEMS:
        return _INVALID_CONFLICT_VALUE
    out: dict[str, str] = {}
    for raw_date, raw_amount in value.items():
        safe_date = _safe_iso_date(raw_date)
        safe_amount = _safe_money(raw_amount)
        if not isinstance(safe_date, str) or not isinstance(safe_amount, str):
            return _INVALID_CONFLICT_VALUE
        out[safe_date] = safe_amount
    return dict(sorted(out.items()))


def _safe_daily_prices(value: Any) -> dict[str, dict[str, str]] | None | object:
    if value is None:
        return None
    if not isinstance(value, Mapping) or len(value) > 64:
        return _INVALID_CONFLICT_VALUE
    out: dict[str, dict[str, str]] = {}
    for raw_order_room_id, raw_prices in value.items():
        if (
            not isinstance(raw_order_room_id, str)
            or not _ORDER_ROOM_ID_RE.fullmatch(raw_order_room_id)
            or _PHONE_LIKE_RE.search(raw_order_room_id)
        ):
            return _INVALID_CONFLICT_VALUE
        safe_prices = _safe_daily_price_values(raw_prices)
        if not isinstance(safe_prices, dict):
            return _INVALID_CONFLICT_VALUE
        out[raw_order_room_id] = safe_prices
    return dict(sorted(out.items()))


def _safe_stay_structure(value: Any) -> str | None | object:
    if value is None:
        return None
    return (
        value
        if isinstance(value, str)
        and _STAY_GROUP_ID_RE.fullmatch(value)
        and not _PHONE_LIKE_RE.search(value)
        else _INVALID_CONFLICT_VALUE
    )


def _safe_conflict_value(field: Any, value: Any) -> Any:
    if not isinstance(field, str) or field not in _KNOWN_CONFLICT_FIELDS:
        return _REDACTED
    if field in _SENSITIVE_CONFLICT_FIELDS:
        return _REDACTED
    if field in {"check_in_date", "check_out_date"}:
        safe = _safe_iso_date(value)
    elif field == "room_assignment":
        safe = _safe_room_assignment(value)
    elif field == "stay_structure":
        safe = _safe_stay_structure(value)
    elif field in {"actual_price", "ota_owner_revenue"}:
        safe = _safe_money(value)
    elif field == "daily_prices":
        safe = _safe_daily_prices(value)
    elif field == "channel":
        safe = (
            value
            if value is None
            or (isinstance(value, str) and value in _SAFE_CHANNELS)
            else _INVALID_CONFLICT_VALUE
        )
    else:
        safe = (
            value
            if value is None
            or (isinstance(value, str) and value in _SAFE_ORDER_STATUSES)
            else _INVALID_CONFLICT_VALUE
        )
    return (
        _REDACTED
        if safe is _INVALID_CONFLICT_VALUE
        or not _fits_journal_json_byte_limit(safe)
        else safe
    )


def _safe_conflict_id(value: Any) -> str:
    return (
        value
        if isinstance(value, str) and _CONFLICT_ID_RE.fullmatch(value)
        else "unknown"
    )


def _safe_source_order_id(value: Any) -> str:
    return (
        value
        if isinstance(value, str) and _ORDER_ID_RE.fullmatch(value)
        else "unknown"
    )


def _fingerprints(raw_key: str) -> tuple[str, str]:
    try:
        raw = raw_key.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ValueError("invalid idempotency key") from exc
    if not raw_key.strip() or len(raw) > 1024:
        raise ValueError("invalid idempotency key")
    configured_key = settings.BYPMS_RETRY_HMAC_KEY
    raw_hmac_key = (
        configured_key.get_secret_value()
        if hasattr(configured_key, "get_secret_value")
        else configured_key
    )
    hmac_key = raw_hmac_key.encode("utf-8")
    if len(hmac_key) < 32:
        raise RetryUnavailableError from None
    hmac_digest = hmac.new(
        hmac_key,
        _IDEMPOTENCY_DOMAIN + raw,
        hashlib.sha256,
    ).hexdigest()
    legacy_digest = hashlib.sha256(_IDEMPOTENCY_DOMAIN + raw).hexdigest()
    return (
        f"{_HMAC_SCHEME}:{hmac_digest}",
        f"{_LEGACY_SCHEME}:{legacy_digest}",
    )


def _trusted_lower_hex(value, *, prefix: str, digest_length: int):
    digest = func.substr(value, len(prefix) + 1, digest_length)
    non_hex = digest
    for character in "0123456789abcdef":
        non_hex = func.replace(non_hex, character, "")
    return and_(
        func.length(value) == len(prefix) + digest_length,
        func.substr(value, 1, len(prefix)) == prefix,
        non_hex == "",
    )


def _trusted_digest_predicate(scheme: str):
    prefix = f"{scheme}:"
    return and_(
        bypms_sync_requests.c.idempotency_scheme == scheme,
        _trusted_lower_hex(
            bypms_sync_requests.c.idempotency_key,
            prefix=prefix,
            digest_length=64,
        ),
    )


def _trusted_request_predicate():
    return and_(
        or_(
            _trusted_digest_predicate(_HMAC_SCHEME),
            _trusted_digest_predicate(_LEGACY_SCHEME),
        ),
        bypms_sync_requests.c.requested_by.in_(tuple(sorted(_SAFE_REQUEST_ACTORS))),
        bypms_sync_requests.c.status.in_(tuple(sorted(_SAFE_REQUEST_STATUSES))),
        _trusted_lower_hex(
            bypms_sync_requests.c.request_id,
            prefix="REQ-",
            digest_length=20,
        ),
    )


def _retry_response(row: Mapping[str, Any]) -> BypmsSyncRetryResponse:
    request_id = row["request_id"]
    requested_by = row["requested_by"]
    status = row["status"]
    requested_at = _as_utc(row["requested_at"])
    if (
        not isinstance(request_id, str)
        or not _REQUEST_ID_RE.fullmatch(request_id)
        or requested_by not in _SAFE_REQUEST_ACTORS
        or status not in _SAFE_REQUEST_STATUSES
        or requested_at is None
    ):
        raise RetryUnavailableError
    return BypmsSyncRetryResponse(
        request_id=request_id,
        requested_by=requested_by,
        status=status,
        requested_at=requested_at,
    )


def _safe_step_summary(
    value: Any,
    *,
    step_name: str,
    step_status: str,
) -> BypmsSyncStepSummary | None:
    bounded = _bounded_json(value)
    if not isinstance(bounded, dict):
        return None

    raw_counts = bounded.get("counts")
    counts: dict[str, int] = {}
    if isinstance(raw_counts, dict):
        counts = {
            key: count
            for key, count in raw_counts.items()
            if (
                key in _SAFE_COUNT_FIELDS
                and isinstance(count, int)
                and not isinstance(count, bool)
                and -(2**63) <= count <= (2**63 - 1)
            )
        }

    summary_status = bounded.get("status")
    if not isinstance(summary_status, str) or summary_status not in _SUMMARY_STATUSES:
        summary_status = {
            "succeeded": "success",
            "failed": "failure",
            "skipped": "skipped",
        }.get(step_status)
    if summary_status not in _SUMMARY_STATUSES:
        return None

    raw_error_code = bounded.get("error_code")
    error_code = (
        raw_error_code
        if isinstance(raw_error_code, str) and raw_error_code in _SAFE_ERROR_CODES
        else None
    )
    raw_duration = bounded.get("duration_ms")
    duration_ms = (
        raw_duration
        if isinstance(raw_duration, int)
        and not isinstance(raw_duration, bool)
        and 0 <= raw_duration <= 604_800_000
        else 0
    )
    return BypmsSyncStepSummary(
        name=step_name,
        status=summary_status,
        counts=counts,
        error_code=error_code,
        duration_ms=duration_ms,
    )


def _is_missing_table_error(exc: DBAPIError, db: AsyncSession) -> bool:
    original = exc.orig
    sqlstate = (
        getattr(original, "sqlstate", None)
        or getattr(original, "pgcode", None)
        or getattr(getattr(original, "__cause__", None), "sqlstate", None)
    )
    if sqlstate in {"42P01", "42703"}:
        return True
    dialect = getattr(getattr(db, "bind", None), "dialect", None)
    sqlite_message = str(original).lower()
    return (
        getattr(dialect, "name", None) == "sqlite"
        and isinstance(original, sqlite3.OperationalError)
        and (
            sqlite_message.startswith("no such table:")
            or sqlite_message.startswith("no such column:")
        )
    )


async def _execute_integration(db: AsyncSession, statement):
    try:
        return await db.execute(statement)
    except DBAPIError as exc:
        if not _is_missing_table_error(exc, db):
            raise
        logger.info(
            "BYPMS integration read tables unavailable (%s)",
            exc.orig.__class__.__name__,
        )
        raise IntegrationTablesUnavailable from None


def _cycle_from_row(row: Mapping[str, Any], now: datetime) -> BypmsSyncCycle:
    status = str(row["status"])
    if status not in _CYCLE_STATUSES:
        raise ValueError("unsupported BYPMS cycle status")
    started_at = _as_utc(row["started_at"])
    finished_at = _as_utc(row["finished_at"])
    assert started_at is not None
    return BypmsSyncCycle(
        cycle_id=_safe_cycle_id(row["cycle_id"]),
        trigger=_safe_trigger(row["trigger"]),
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=_duration_ms(started_at, finished_at, now),
        write_modes=_safe_write_modes(row["write_modes"]),
    )


def _step_from_row(row: Mapping[str, Any], now: datetime) -> BypmsSyncStep:
    status = str(row["status"])
    if status not in _STEP_STATUSES:
        raise ValueError("unsupported BYPMS step status")
    started_at = _as_utc(row["started_at"])
    finished_at = _as_utc(row["finished_at"])
    assert started_at is not None
    name = _safe_step_name(row["name"])
    return BypmsSyncStep(
        step_id=int(row["step_id"]),
        name=name,
        status=status,
        started_at=started_at,
        finished_at=finished_at,
        duration_ms=_duration_ms(started_at, finished_at, now),
        summary=_safe_step_summary(
            row["summary"], step_name=name, step_status=status
        ),
    )


def _unavailable_overview() -> BypmsSyncOverview:
    return BypmsSyncOverview(
        available=False,
        message=ROLLOUT_MESSAGE,
        stale_after_seconds=_stale_after_seconds(),
        clock_skew_detected=False,
    )


def _stale_after_seconds() -> int:
    """Expose the watchdog's validated deployment threshold to API clients."""
    return settings.BYPMS_PULL_STALE_ALERT_MINUTES * 60


def _unavailable_cycles() -> BypmsSyncCyclesResponse:
    return BypmsSyncCyclesResponse(
        available=False,
        message=ROLLOUT_MESSAGE,
        items=[],
    )


async def get_overview(db: AsyncSession, now: datetime) -> BypmsSyncOverview:
    """Build the bounded overview without selecting integration payload or PII columns."""
    now = _as_utc(now) or datetime.now(timezone.utc)
    cycle_columns = (
        bypms_sync_cycles.c.cycle_id,
        bypms_sync_cycles.c.trigger,
        bypms_sync_cycles.c.write_modes,
        bypms_sync_cycles.c.status,
        bypms_sync_cycles.c.started_at,
        bypms_sync_cycles.c.finished_at,
    )
    try:
        latest_row = (
            await _execute_integration(
                db,
                select(*cycle_columns)
                .order_by(
                    bypms_sync_cycles.c.started_at.desc(),
                    bypms_sync_cycles.c.cycle_id.desc(),
                )
                .limit(1),
            )
        ).mappings().first()
        last_good_row = (
            await _execute_integration(
                db,
                select(
                    bypms_sync_cycles.c.cycle_id,
                    bypms_sync_cycles.c.finished_at,
                )
                .where(
                    bypms_sync_cycles.c.status.in_(("succeeded", "partial")),
                    bypms_sync_cycles.c.finished_at.is_not(None),
                )
                .order_by(
                    bypms_sync_cycles.c.finished_at.desc(),
                    bypms_sync_cycles.c.cycle_id.desc(),
                )
                .limit(1),
            )
        ).mappings().first()
        last_full_at = _as_utc((await _execute_integration(
            db, select(func.max(bypms_sync_cycles.c.finished_at)).where(
                bypms_sync_cycles.c.status == "succeeded",
            ),
        )).scalar_one_or_none())
        completed_cycle_id = (
            await _execute_integration(
                db,
                select(bypms_sync_cycles.c.cycle_id)
                .where(
                    bypms_sync_cycles.c.status.in_(("succeeded", "partial", "failed")),
                    bypms_sync_cycles.c.finished_at.is_not(None),
                )
                .order_by(
                    bypms_sync_cycles.c.finished_at.desc(),
                    bypms_sync_cycles.c.cycle_id.desc(),
                )
                .limit(1),
            )
        ).scalar_one_or_none()
        pending_retry_count = int((
            await _execute_integration(
                db,
                select(func.count(bypms_sync_requests.c.request_id)).where(
                    bypms_sync_requests.c.status.in_(("queued", "running"))
                ),
            )
        ).scalar_one())
        await _execute_integration(
            db,
            select(bypms_sync_requests.c.idempotency_scheme).limit(0),
        )
        await _execute_integration(
            db,
            select(bypms_sync_steps.c.step_id).limit(0),
        )
        staging_watermark = _as_utc((
            await _execute_integration(
                db,
                select(func.max(ota_raw_orders.c.fetched_at)).where(
                    ota_raw_orders.c.platform == "bypms"
                ),
            )
        ).scalar_one_or_none())
    except IntegrationTablesUnavailable:
        return _unavailable_overview()

    conflict_rows = (
        await db.execute(
            select(OrderSyncConflict.field, func.count(OrderSyncConflict.conflict_id))
            .where(
                OrderSyncConflict.status == OrderSyncConflictStatus.open,
                OrderSyncConflict.field.in_(_KNOWN_CONFLICT_FIELDS),
            )
            .group_by(OrderSyncConflict.field)
            .order_by(OrderSyncConflict.field)
        )
    ).all()
    conflicts = {str(field): int(count) for field, count in conflict_rows}

    latest_clock_skew = bool(
        latest_row
        and (
            _is_future_beyond_clock_skew_tolerance(latest_row["started_at"], now)
            or _is_future_beyond_clock_skew_tolerance(latest_row["finished_at"], now)
        )
    )
    last_good_at = _as_utc(last_good_row["finished_at"]) if last_good_row else None
    last_good_clock_skew = _is_future_beyond_clock_skew_tolerance(last_good_at, now)
    staging_clock_skew = _is_future_beyond_clock_skew_tolerance(
        staging_watermark, now
    )
    clock_skew_detected = (
        latest_clock_skew or last_good_clock_skew or staging_clock_skew
        or _is_future_beyond_clock_skew_tolerance(last_full_at, now)
    )
    latest_cycle = (
        _cycle_from_row(latest_row, now)
        if latest_row and not latest_clock_skew
        else None
    )
    return BypmsSyncOverview(
        available=True,
        message=None,
        stale_after_seconds=_stale_after_seconds(),
        clock_skew_detected=clock_skew_detected,
        latest_cycle=latest_cycle,
        last_successful_or_partial_at=last_good_at,
        last_successful_or_partial_age_seconds=_age_seconds_or_none(
            last_good_at, now
        ),
        last_fully_successful_at=last_full_at,
        last_fully_successful_age_seconds=_age_seconds_or_none(last_full_at, now),
        pending_retry_count=pending_retry_count,
        open_conflicts_by_field=conflicts,
        staging_watermark=staging_watermark,
        staging_lag_seconds=_age_seconds_or_none(staging_watermark, now),
        write_modes=latest_cycle.write_modes if latest_cycle else {},
        retry_available=completed_cycle_id is not None,
    )


async def list_cycles(
    db: AsyncSession,
    now: datetime,
    *,
    limit: int,
    status: str | None,
) -> BypmsSyncCyclesResponse:
    """List a bounded number of cycles and their ordered, sanitized step outcomes."""
    now = _as_utc(now) or datetime.now(timezone.utc)
    if limit < 1 or limit > 100:
        raise ValueError("limit must be between 1 and 100")
    if status is not None and status not in _CYCLE_STATUSES:
        raise ValueError("unsupported BYPMS cycle status")

    cycle_statement = select(
        bypms_sync_cycles.c.cycle_id,
        bypms_sync_cycles.c.trigger,
        bypms_sync_cycles.c.write_modes,
        bypms_sync_cycles.c.status,
        bypms_sync_cycles.c.started_at,
        bypms_sync_cycles.c.finished_at,
    )
    if status is not None:
        cycle_statement = cycle_statement.where(bypms_sync_cycles.c.status == status)
    cycle_statement = cycle_statement.order_by(
        bypms_sync_cycles.c.started_at.desc(),
        bypms_sync_cycles.c.cycle_id.desc(),
    ).limit(limit)

    try:
        cycle_rows = (
            await _execute_integration(db, cycle_statement)
        ).mappings().all()
        cycle_ids = [str(row["cycle_id"]) for row in cycle_rows]
        step_rows = []
        if cycle_ids:
            step_rows = (
                await _execute_integration(
                    db,
                    select(
                        bypms_sync_steps.c.step_id,
                        bypms_sync_steps.c.cycle_id,
                        bypms_sync_steps.c.name,
                        bypms_sync_steps.c.status,
                        bypms_sync_steps.c.started_at,
                        bypms_sync_steps.c.finished_at,
                        bypms_sync_steps.c.summary,
                    )
                    .where(
                        bypms_sync_steps.c.cycle_id.in_(cycle_ids),
                        bypms_sync_steps.c.name.in_(tuple(sorted(_STEP_NAMES))),
                    )
                    .order_by(
                        bypms_sync_steps.c.cycle_id,
                        bypms_sync_steps.c.started_at,
                        bypms_sync_steps.c.step_id,
                    )
                    .limit(limit * len(_STEP_NAMES)),
                )
            ).mappings().all()
    except IntegrationTablesUnavailable:
        return _unavailable_cycles()

    steps_by_cycle: dict[str, list[BypmsSyncStep]] = {
        cycle_id: [] for cycle_id in cycle_ids
    }
    for row in step_rows:
        steps_by_cycle[str(row["cycle_id"])].append(_step_from_row(row, now))

    items: list[BypmsSyncCycleWithSteps] = []
    for row in cycle_rows:
        cycle = _cycle_from_row(row, now)
        raw_cycle_id = str(row["cycle_id"])
        items.append(BypmsSyncCycleWithSteps(
            **cycle.model_dump(),
            steps=steps_by_cycle[raw_cycle_id],
        ))
    return BypmsSyncCyclesResponse(available=True, message=None, items=items)


async def list_conflicts(
    db: AsyncSession,
    *,
    field: str | None,
    status: str,
    page: int,
    page_size: int,
) -> BypmsSyncConflictsResponse:
    """List global conflict facts without selecting guest, phone, note, or payload columns."""
    if field is not None and field not in _KNOWN_CONFLICT_FIELDS:
        raise ValueError("unsupported BYPMS conflict field")
    if status not in _CONFLICT_STATUSES:
        raise ValueError("unsupported BYPMS conflict status")
    if page < 1 or page > 1_000_000 or page_size < 1 or page_size > 100:
        raise ValueError("invalid conflict page")

    conditions = [OrderSyncConflict.status == OrderSyncConflictStatus(status)]
    if field is not None:
        conditions.append(OrderSyncConflict.field == field)

    total = int((await db.execute(
        select(func.count(OrderSyncConflict.conflict_id))
        .select_from(OrderSyncConflict)
        .join(Order, Order.order_id == OrderSyncConflict.source_order_id)
        .where(*conditions)
    )).scalar_one())
    rows = (await db.execute(
        select(
            OrderSyncConflict.conflict_id,
            OrderSyncConflict.source_order_id,
            Order.channel,
            Order.check_in_date,
            Order.check_out_date,
            OrderSyncConflict.field,
            OrderSyncConflict.local_value,
            OrderSyncConflict.upstream_value,
            OrderSyncConflict.status,
            OrderSyncConflict.first_seen_at,
            OrderSyncConflict.last_seen_at,
        )
        .join(Order, Order.order_id == OrderSyncConflict.source_order_id)
        .where(*conditions)
        .order_by(
            OrderSyncConflict.last_seen_at.desc(),
            OrderSyncConflict.conflict_id.asc(),
        )
        .offset((page - 1) * page_size)
        .limit(page_size)
    )).mappings().all()

    items: list[BypmsSyncConflict] = []
    for row in rows:
        raw_field = row["field"]
        safe_field = (
            raw_field
            if isinstance(raw_field, str) and raw_field in _KNOWN_CONFLICT_FIELDS
            else "unknown"
        )
        raw_status = row["status"]
        safe_status = (
            raw_status.value
            if isinstance(raw_status, OrderSyncConflictStatus)
            else str(raw_status)
        )
        items.append(BypmsSyncConflict(
            conflict_id=_safe_conflict_id(row["conflict_id"]),
            source_order_id=_safe_source_order_id(row["source_order_id"]),
            channel=_safe_channel(row["channel"]),
            check_in_date=row["check_in_date"],
            check_out_date=row["check_out_date"],
            field=safe_field,
            local_value=_safe_conflict_value(raw_field, row["local_value"]),
            upstream_value=_safe_conflict_value(raw_field, row["upstream_value"]),
            status=safe_status,
            first_seen_at=_as_utc(row["first_seen_at"]),
            last_seen_at=_as_utc(row["last_seen_at"]),
        ))
    return BypmsSyncConflictsResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


async def enqueue_retry_request(
    db: AsyncSession,
    *,
    raw_idempotency_key: str,
    operator_id: str,
    now: datetime | None = None,
) -> BypmsSyncRetryResponse:
    """Atomically enqueue one trusted retry and its human-attributed PMS audit row."""
    hmac_fingerprint, legacy_fingerprint = _fingerprints(raw_idempotency_key)
    requested_at = _as_utc(now) or datetime.now(timezone.utc)
    queue_schema_operation = True
    try:
        async with db.begin():
            dialect = getattr(getattr(db, "bind", None), "dialect", None)
            if getattr(dialect, "name", None) == "postgresql":
                await db.execute(
                    select(func.pg_advisory_xact_lock(_RETRY_ADVISORY_LOCK_KEY))
                )

            trusted = _trusted_request_predicate()
            untrusted_id = (await db.execute(
                select(bypms_sync_requests.c.request_id)
                .where(~trusted)
                .limit(1)
            )).scalar_one_or_none()
            if untrusted_id is not None:
                raise RetryUnavailableError

            existing_row = (await db.execute(
                select(
                    bypms_sync_requests.c.request_id,
                    bypms_sync_requests.c.requested_by,
                    bypms_sync_requests.c.status,
                    bypms_sync_requests.c.requested_at,
                )
                .where(or_(
                    and_(
                        bypms_sync_requests.c.idempotency_scheme == _LEGACY_SCHEME,
                        bypms_sync_requests.c.idempotency_key == legacy_fingerprint,
                    ),
                    and_(
                        bypms_sync_requests.c.idempotency_scheme == _HMAC_SCHEME,
                        bypms_sync_requests.c.idempotency_key == hmac_fingerprint,
                    ),
                ))
                .order_by(case(
                    (bypms_sync_requests.c.idempotency_key == legacy_fingerprint, 0),
                    else_=1,
                ))
                .limit(1)
            )).mappings().first()
            if existing_row is not None:
                return _retry_response(existing_row)

            active_id = (await db.execute(
                select(bypms_sync_requests.c.request_id)
                .where(bypms_sync_requests.c.status.in_(("queued", "running")))
                .limit(1)
            )).scalar_one_or_none()
            if active_id is not None:
                raise RetryAlreadyActiveError

            request_id = f"REQ-{secrets.token_hex(10)}"
            await db.execute(insert(bypms_sync_requests).values(
                request_id=request_id,
                requested_by="pms_admin",
                idempotency_key=hmac_fingerprint,
                idempotency_scheme=_HMAC_SCHEME,
                status="queued",
                cycle_id=None,
                requested_at=requested_at,
                started_at=None,
                finished_at=None,
            ))
            queue_schema_operation = False
            await log_action_tx(
                db,
                operator_id=operator_id,
                action="bypms_sync.retry_requested",
                resource_type="bypms_sync_request",
                resource_id=request_id,
                after_data={
                    "request_id": request_id,
                    "requested_by": "pms_admin",
                    "status": "queued",
                },
            )
            response = BypmsSyncRetryResponse(
                request_id=request_id,
                requested_by="pms_admin",
                status="queued",
                requested_at=requested_at,
            )
        return response
    except (RetryUnavailableError, RetryAlreadyActiveError):
        raise
    except DBAPIError as exc:
        if queue_schema_operation and _is_missing_table_error(exc, db):
            logger.info(
                "BYPMS retry queue unavailable (%s)",
                exc.orig.__class__.__name__,
            )
            raise RetryUnavailableError from None
        logger.error(
            "BYPMS retry enqueue database failure (%s)",
            exc.orig.__class__.__name__,
        )
        raise RetryRequestFailedError from None
