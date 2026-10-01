"""Durable, actor-authorized monthly-close event append and replay.

The database sequence is the cycle-wide cursor. Authorization is deliberately
applied after a bounded sequence scan so hidden rows still advance the cursor
without contributing any public metadata.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import hashlib
import json
import re
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESSIV
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.monthly_close import MonthlyCloseCycle
from app.models.monthly_close_control import MonthlyCloseEvent


EVENT_RETENTION_DAYS = 180
MAX_EVENT_PAGE_SIZE = 100
EMPLOYEE_ROLES = frozenset({"admin", "finance", "operator", "cleaner", "keeper"})
EVENT_VISIBILITY: dict[str, str] = {
    "file.stored": "operations",
    "analysis.completed": "operations",
    "assistant.received": "assigned_staff",
    "assistant.replied": "assigned_staff",
    "reconciliation.completed": "finance",
}
class _EventPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")
    actor_id: str | None = None

    @field_validator("actor_id")
    @classmethod
    def validate_actor_id(cls, value: str | None) -> str | None:
        return _safe_identifier(value) if value is not None else None


class FileStoredPayload(_EventPayload):
    document_id: str
    source_type: str | None = None

    @field_validator("document_id", "source_type")
    @classmethod
    def validate_identifiers(cls, value: str | None) -> str | None:
        return _safe_identifier(value) if value is not None else None


class AnalysisCompletedPayload(_EventPayload):
    document_id: str

    @field_validator("document_id")
    @classmethod
    def validate_document_id(cls, value: str) -> str:
        return _safe_identifier(value)


class AssistantReceivedPayload(_EventPayload):
    message_id: str
    run_id: str

    @field_validator("message_id", "run_id")
    @classmethod
    def validate_identifiers(cls, value: str) -> str:
        return _safe_identifier(value)


class AssistantRepliedPayload(AssistantReceivedPayload):
    intent: str
    narration_degraded: bool

    @field_validator("intent")
    @classmethod
    def validate_intent(cls, value: str) -> str:
        if value not in {"read_query", "clarification", "write_request", "sensitive_input", "action_plan", "action_result"}:
            raise ValueError("invalid assistant event intent")
        return value


class ReconciliationCompletedPayload(_EventPayload):
    result_code: str

    @field_validator("result_code")
    @classmethod
    def validate_result_code(cls, value: str) -> str:
        if value not in {"matched", "differences_found", "needs_review"}:
            raise ValueError("invalid reconciliation result code")
        return value


EVENT_PAYLOAD_MODELS: dict[str, type[_EventPayload]] = {
    "file.stored": FileStoredPayload,
    "analysis.completed": AnalysisCompletedPayload,
    "assistant.received": AssistantReceivedPayload,
    "assistant.replied": AssistantRepliedPayload,
    "reconciliation.completed": ReconciliationCompletedPayload,
}


class EventCursorExpired(RuntimeError):
    """The cursor predates the cycle's hot replay boundary."""


class EventCursorInvalid(ValueError):
    """The cursor cannot identify a valid boundary in this cycle."""


class EventCursorRefreshRequired(RuntimeError):
    """A well-formed opaque cursor can no longer be authenticated."""


def _safe_identifier(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", value
    ):
        raise ValueError("event identifier is not safe")
    return value


def _typed_event_payload(
    kind: str, payload: dict[str, Any], actor_id: str
) -> dict[str, Any]:
    if any(re.sub(r"[^a-z0-9]", "", str(key).lower()) == "actorid" for key in payload):
        raise ValueError("event actor attribution is server-owned")
    model = EVENT_PAYLOAD_MODELS[kind]
    try:
        validated = model.model_validate({**payload, "actor_id": actor_id})
    except ValidationError as exc:
        raise ValueError("monthly-close event payload is not allowlisted") from exc
    return validated.model_dump(mode="json", exclude_none=True)


@dataclass(frozen=True)
class CycleEventView:
    event_id: str
    kind: str
    schema_version: str
    payload: dict[str, Any]
    occurred_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "event_id": self.event_id,
            "kind": self.kind,
            "schema_version": self.schema_version,
            "payload": self.payload,
            "occurred_at": self.occurred_at,
        }


@dataclass(frozen=True)
class CycleEventPage:
    events: tuple[CycleEventView, ...]
    next_cursor: str
    advanced: bool = False

    def __iter__(self):
        return iter(self.events)

    def to_dict(self) -> dict[str, Any]:
        return {
            "events": [event.to_dict() for event in self.events],
            "next_cursor": self.next_cursor,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), ensure_ascii=False, separators=(",", ":"))


def _actor_identity(actor: Any) -> tuple[str, str]:
    if isinstance(actor, dict):
        actor_id = str(actor.get("user_id") or "")
        raw_role = actor.get("role")
    else:
        actor_id = str(getattr(actor, "user_id", ""))
        raw_role = getattr(actor, "role", None)
    role = str(getattr(raw_role, "value", raw_role) or "")
    if not actor_id or role not in EMPLOYEE_ROLES:
        raise ValueError("monthly-close events require a current employee actor")
    return actor_id, role


def _restricted_cursor(cycle_id: str, actor_id: str, role: str, sequence: int) -> str:
    key = hashlib.sha512(
        settings.JWT_SECRET_KEY.encode("utf-8")
        + b"\x00monthly-close-event-cursor-aes-siv-v2"
    ).digest()
    body = json.dumps(
        {
            "actor_id": actor_id,
            "cycle_id": cycle_id,
            "role": role,
            # Fixed width prevents token length from revealing sequence scale.
            "sequence": f"{sequence:020d}",
            "version": 2,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    ciphertext = AESSIV(key).encrypt(
        body, [b"monthly-close-event-cursor-public-v2"]
    )
    encoded = base64.urlsafe_b64encode(ciphertext).decode("ascii")
    return "mce2." + encoded


def public_cycle_cursor(
    cycle_id: str, actor: Any, sequence: int, *, empty_zero: bool = False
) -> str:
    """Return the role-appropriate public string cursor for a DB sequence."""
    actor_id, role = _actor_identity(actor)
    if sequence == 0 and empty_zero:
        return ""
    if role == "admin":
        return str(sequence)
    return _restricted_cursor(cycle_id, actor_id, role, sequence)


def _resolve_cursor(cycle_id: str, actor: Any, cursor: str | None) -> int:
    actor_id, role = _actor_identity(actor)
    value = (cursor or "").strip()
    if not value:
        return 0
    if role == "admin":
        if not value.isascii() or not value.isdigit():
            raise EventCursorInvalid("event cursor is invalid")
        return int(value)
    try:
        prefix, encrypted = value.split(".", 1)
        if prefix not in {"mce1", "mce2"}:
            raise EventCursorInvalid("event cursor is invalid")
        if prefix == "mce1":
            raise EventCursorRefreshRequired("event cursor key version is retired")
        key = hashlib.sha512(
            settings.JWT_SECRET_KEY.encode("utf-8")
            + b"\x00monthly-close-event-cursor-aes-siv-v2"
        ).digest()
        try:
            ciphertext = base64.urlsafe_b64decode(encrypted.encode("ascii"))
            body = AESSIV(key).decrypt(
                ciphertext, [b"monthly-close-event-cursor-public-v2"]
            )
        except (InvalidTag, ValueError) as exc:
            raise EventCursorRefreshRequired(
                "event cursor can no longer be authenticated"
            ) from exc
        payload = json.loads(body)
        if payload != {
            "actor_id": actor_id,
            "cycle_id": cycle_id,
            "role": role,
            "sequence": payload.get("sequence"),
            "version": 2,
        }:
            raise ValueError
        encoded_sequence = payload["sequence"]
        if not isinstance(encoded_sequence, str) or not re.fullmatch(
            r"\d{20}", encoded_sequence
        ):
            raise ValueError
        return int(encoded_sequence)
    except EventCursorRefreshRequired:
        raise
    except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
        raise EventCursorInvalid("event cursor is invalid") from exc


def _can_view(event: MonthlyCloseEvent, actor_id: str, role: str) -> bool:
    if role == "admin":
        return True
    if event.visibility_scope == "finance":
        return role == "finance"
    if event.visibility_scope == "operations":
        return role in {"finance", "operator"}
    if event.visibility_scope == "assigned_staff":
        return str(event.payload_redacted.get("actor_id") or "") == actor_id
    return False


async def append_cycle_event(
    db: AsyncSession,
    cycle_id: str,
    kind: str,
    actor: Any,
    payload: dict[str, Any],
    dedupe_key: str,
) -> MonthlyCloseEvent:
    """Append one allowlisted event under a locked cycle sequence.

    The caller owns the transaction. Locking the cycle row serializes sequence
    allocation on PostgreSQL; a same-dedupe caller returns the committed event.
    """
    actor_id, _role = _actor_identity(actor)
    if kind not in EVENT_VISIBILITY:
        raise ValueError("unsupported monthly-close event kind")
    if not isinstance(payload, dict):
        raise ValueError("monthly-close event payload must be an object")
    if not dedupe_key or len(dedupe_key) > 160:
        raise ValueError("monthly-close event dedupe key is invalid")

    locked_cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle_id)
        .with_for_update()
    )
    if locked_cycle is None:
        raise ValueError("monthly-close cycle does not exist")
    existing = await db.scalar(
        select(MonthlyCloseEvent).where(
            MonthlyCloseEvent.cycle_id == cycle_id,
            MonthlyCloseEvent.dedupe_key == dedupe_key,
        )
    )
    if existing is not None:
        return existing
    maximum = await db.scalar(
        select(func.coalesce(func.max(MonthlyCloseEvent.sequence), 0)).where(
            MonthlyCloseEvent.cycle_id == cycle_id
        )
    )
    safe_payload = _typed_event_payload(kind, payload, actor_id)
    event = MonthlyCloseEvent(
        event_id="MCE-" + uuid4().hex[:20].upper(),
        cycle_id=cycle_id,
        sequence=int(maximum or 0) + 1,
        event_type=kind,
        schema_version="v1",
        payload_redacted=safe_payload,
        visibility_scope=EVENT_VISIBILITY[kind],
        dedupe_key=dedupe_key,
        occurred_at=datetime.now(timezone.utc),
    )
    db.add(event)
    await db.flush()
    return event


async def cycle_event_high_watermark(db: AsyncSession, cycle_id: str) -> int:
    return int(
        await db.scalar(
            select(func.coalesce(func.max(MonthlyCloseEvent.sequence), 0)).where(
                MonthlyCloseEvent.cycle_id == cycle_id
            )
        )
        or 0
    )


async def lock_cycle_for_snapshot(
    db: AsyncSession, cycle_id: str
) -> MonthlyCloseCycle | None:
    """Serialize a projection snapshot with event/domain writers for this cycle."""
    return await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle_id)
        .with_for_update()
    )


def _serialized_event_payload(event: MonthlyCloseEvent) -> dict[str, Any] | None:
    """Validate persisted structured content again at the public boundary."""
    model = EVENT_PAYLOAD_MODELS.get(event.event_type)
    if model is None:
        return None
    try:
        payload = model.model_validate(event.payload_redacted).model_dump(
            mode="json", exclude_none=True
        )
        if event.visibility_scope != "assigned_staff":
            payload.pop("actor_id", None)
        return payload
    except ValidationError:
        return None


async def list_cycle_events(
    db: AsyncSession,
    cycle_id: str,
    actor: Any,
    after_cursor: str | None,
    limit: int,
) -> CycleEventPage:
    """Scan a sequence page and then project only currently authorized rows."""
    actor_id, role = _actor_identity(actor)
    after_sequence = _resolve_cursor(cycle_id, actor, after_cursor)
    if limit < 1 or limit > MAX_EVENT_PAGE_SIZE:
        raise EventCursorInvalid("event cursor or page size is invalid")
    if await db.get(MonthlyCloseCycle, cycle_id) is None:
        raise EventCursorInvalid("event cursor is invalid")
    maximum = await cycle_event_high_watermark(db, cycle_id)
    if after_sequence > maximum:
        raise EventCursorInvalid("event cursor is outside this cycle")

    cutoff = datetime.now(timezone.utc) - timedelta(days=EVENT_RETENTION_DAYS)
    archived_max = await db.scalar(
        select(func.max(MonthlyCloseEvent.sequence)).where(
            MonthlyCloseEvent.cycle_id == cycle_id,
            MonthlyCloseEvent.persisted_at < cutoff,
        )
    )
    if archived_max is not None and after_sequence < int(archived_max):
        raise EventCursorExpired("event cursor is outside the hot retention window")

    scanned = list(
        await db.scalars(
            select(MonthlyCloseEvent)
            .where(
                MonthlyCloseEvent.cycle_id == cycle_id,
                MonthlyCloseEvent.sequence > after_sequence,
            )
            .order_by(MonthlyCloseEvent.sequence)
            .limit(limit)
        )
    )
    views: list[CycleEventView] = []
    for event in scanned:
        safe_payload = _serialized_event_payload(event)
        if not _can_view(event, actor_id, role) or safe_payload is None:
            continue
        views.append(
            CycleEventView(
                event_id=public_cycle_cursor(cycle_id, actor, event.sequence),
                kind=event.event_type,
                schema_version=event.schema_version,
                payload=safe_payload,
                occurred_at=(
                    event.occurred_at
                    if event.occurred_at.tzinfo
                    else event.occurred_at.replace(tzinfo=timezone.utc)
                )
                .astimezone(timezone.utc)
                .isoformat(),
            )
        )
    advanced = bool(scanned)
    next_cursor = (
        public_cycle_cursor(cycle_id, actor, scanned[-1].sequence)
        if advanced
        else (after_cursor or "")
    )
    return CycleEventPage(
        events=tuple(views), next_cursor=next_cursor, advanced=advanced
    )


async def stream_cycle_events(
    db_factory: Any,
    cycle_id: str,
    actor: Any,
    after_cursor: str | None,
) -> AsyncIterator[str]:
    """Yield finite, side-effect-free SSE replay through the JSON projection."""
    async with db_factory() as db:
        page = await list_cycle_events(
            db, cycle_id, actor, after_cursor, MAX_EVENT_PAGE_SIZE
        )
    for event in page.events:
        data = json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":"))
        yield f"id: {event.event_id}\nevent: {event.kind}\ndata: {data}\n\n"
    if page.advanced:
        # A fixed-shape scan checkpoint lets an all-hidden page advance without
        # revealing any hidden event type, identifier, payload, or timestamp.
        yield f"id: {page.next_cursor}\nevent: cursor\ndata: {{}}\n\n"
