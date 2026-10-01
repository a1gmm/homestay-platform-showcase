"""Safe monthly-close cutover and additive legacy-history projection.

The cycle row is the single write-owner authority.  Historical projection only
adds immutable, deterministic events; it never repairs or rewrites financial
facts from the legacy engines.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from alembic.config import Config as AlembicConfig
from alembic.migration import MigrationContext
from alembic.script import ScriptDirectory
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.audit_log import AuditLog
from app.models.monthly_close import (
    MonthlyCloseCycle,
    MonthlyCloseDocument,
    MonthlyCloseStepConfirmation,
)
from app.models.monthly_close_control import (
    MonthlyCloseEvent,
    MonthlyCloseExecutionAttempt,
    MonthlyCloseOutbox,
    MonthlyCloseRemediation,
    MonthlyCloseVerification,
)
from app.models.recon import ReconBatch
from app.models.user import User, UserRole
from app.services.audit import log_action_tx
from app.services.monthly_close.control import get_command_handler_registry


WRITE_CONTROL_OWNERS = frozenset({"legacy", "assistant"})
UNSAFE_ATTEMPT_STATES = frozenset(
    {"executing", "unknown", "remediation_required"}
)
UNDRAINED_OUTBOX_STATES = frozenset(
    {"pending", "leased", "failed", "dead_letter"}
)
OPEN_REMEDIATION_STATES = frozenset(
    {"open", "investigating", "compensating", "rework"}
)
REQUIRED_ASSISTANT_HANDLERS = frozenset(
    {
        "ota_reconciliation",
        "ota_appeal_adjudication",
        "service_fee_reconciliation",
        "utility_reconciliation",
        "operating_expense_import",
        "generate_owner_settlements",
        "finalize_monthly_close",
    }
)


class MonthlyCloseRolloutError(RuntimeError):
    def __init__(
        self,
        code: str,
        message: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.code = code
        self.message = message
        self.detail = detail or {}
        super().__init__(message)

    def to_detail(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.detail}


@dataclass(frozen=True)
class WriteControlResult:
    cycle_id: str
    owner: Literal["legacy", "assistant"]
    control_version: int
    replayed: bool

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LegacyMigrationResult:
    cycle_id: str
    created_events: int
    existing_events: int


def _actor_id(actor: Any) -> str:
    if isinstance(actor, dict):
        value = actor.get("user_id")
    else:
        value = getattr(actor, "user_id", None)
    if not isinstance(value, str) or not value:
        raise MonthlyCloseRolloutError(
            "write_control_actor_forbidden", "当前管理员身份无效"
        )
    return value


async def _current_admin(db: AsyncSession, actor: Any) -> User:
    actor_id = _actor_id(actor)
    current = await db.scalar(
        select(User)
        .where(User.user_id == actor_id)
        .execution_options(populate_existing=True)
    )
    role = current.role if current is not None else None
    if (
        current is None
        or role != UserRole.admin
        or not current.is_active
    ):
        raise MonthlyCloseRolloutError(
            "write_control_actor_forbidden", "只有当前有效管理员可以切换月结写入控制权"
        )
    return current


def _stable_digest(value: Any) -> str:
    return sha256(
        json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()


def _switch_dedupe_key(
    cycle_id: str, expected_version: int, owner: str, actor_id: str
) -> str:
    digest = _stable_digest(
        {
            "actor_id": actor_id,
            "cycle_id": cycle_id,
            "expected_version": expected_version,
            "owner": owner,
            "version": 1,
        }
    )
    return f"write-control:v1:{digest}"


async def _switch_replay(
    db: AsyncSession,
    *,
    cycle: MonthlyCloseCycle,
    expected_version: int,
    owner: str,
    actor_id: str,
) -> WriteControlResult | None:
    if (
        cycle.control_version != expected_version + 1
        or cycle.write_control_owner != owner
    ):
        return None
    existing = await db.scalar(
        select(MonthlyCloseEvent.event_id).where(
            MonthlyCloseEvent.cycle_id == cycle.cycle_id,
            MonthlyCloseEvent.dedupe_key
            == _switch_dedupe_key(
                cycle.cycle_id, expected_version, owner, actor_id
            ),
            MonthlyCloseEvent.event_type == "write_control.switched",
        )
    )
    if existing is None:
        return None
    return WriteControlResult(
        cycle_id=cycle.cycle_id,
        owner=owner,
        control_version=cycle.control_version,
        replayed=True,
    )


async def require_cycle_write_control(
    db: AsyncSession,
    cycle_id: str,
    *,
    expected_version: int,
    owner: Literal["legacy", "assistant"],
) -> MonthlyCloseCycle:
    """Lock one exact cycle and reject stale or wrong-owner write requests."""
    if owner not in WRITE_CONTROL_OWNERS or expected_version < 1:
        raise MonthlyCloseRolloutError(
            "write_control_request_invalid", "写入控制参数无效"
        )
    cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if cycle is None:
        raise MonthlyCloseRolloutError("cycle_not_found", "月结周期不存在")
    if cycle.control_version != expected_version:
        raise MonthlyCloseRolloutError(
            "control_version_stale",
            "月结写入控制版本已变化，请刷新后重试",
            detail={
                "actual_version": cycle.control_version,
                "expected_version": expected_version,
            },
        )
    if cycle.write_control_owner != owner:
        raise MonthlyCloseRolloutError(
            "write_control_owner_changed",
            "月结写入控制方已变化，请刷新后重试",
            detail={"actual_owner": cycle.write_control_owner},
        )
    return cycle


def _assistant_readiness_errors() -> list[str]:
    failures: list[str] = []
    for name in (
        "MONTHLY_CLOSE_ASSISTANT_ENABLED",
        "MONTHLY_CLOSE_SOURCE_ADAPTERS_ENABLED",
        "MONTHLY_CLOSE_PROPOSAL_EXECUTION_ENABLED",
    ):
        if not bool(getattr(settings, name, False)):
            failures.append(name)
    registry = get_command_handler_registry().as_mapping()
    if not REQUIRED_ASSISTANT_HANDLERS.issubset(registry):
        failures.append("COMMAND_HANDLERS")
    return failures


@lru_cache(maxsize=1)
def repository_alembic_heads() -> tuple[str, ...]:
    """Resolve the application-required schema from the checked-in graph."""
    backend_root = Path(__file__).resolve().parents[3]
    config = AlembicConfig(str(backend_root / "alembic.ini"))
    config.set_main_option(
        "script_location", str(backend_root / "app" / "alembic")
    )
    return tuple(sorted(ScriptDirectory.from_config(config).get_heads()))


async def database_alembic_heads(db: AsyncSession) -> tuple[str, ...]:
    connection = await db.connection()
    return await connection.run_sync(
        lambda sync_connection: tuple(
            sorted(
                MigrationContext.configure(sync_connection).get_current_heads()
            )
        )
    )


async def _schema_is_current(db: AsyncSession) -> bool:
    required = repository_alembic_heads()
    deployed = await database_alembic_heads(db)
    return len(required) == 1 and deployed == required


async def _assert_switch_boundary_drained(
    db: AsyncSession, cycle_id: str
) -> None:
    unsafe_attempts = int(
        await db.scalar(
            select(func.count(MonthlyCloseExecutionAttempt.attempt_id)).where(
                MonthlyCloseExecutionAttempt.cycle_id == cycle_id,
                MonthlyCloseExecutionAttempt.status.in_(UNSAFE_ATTEMPT_STATES),
            )
        )
        or 0
    )
    if unsafe_attempts:
        raise MonthlyCloseRolloutError(
            "write_control_in_flight",
            "仍有执行中、结果未知或待补救的写入，不能切换控制权",
            detail={"blocking_count": unsafe_attempts},
        )
    open_remediations = int(
        await db.scalar(
            select(func.count(MonthlyCloseRemediation.remediation_id)).where(
                MonthlyCloseRemediation.cycle_id == cycle_id,
                MonthlyCloseRemediation.status.in_(OPEN_REMEDIATION_STATES),
            )
        )
        or 0
    )
    inconclusive = int(
        await db.scalar(
            select(func.count(MonthlyCloseVerification.verification_id)).where(
                MonthlyCloseVerification.cycle_id == cycle_id,
                MonthlyCloseVerification.status == "inconclusive",
            )
        )
        or 0
    )
    if open_remediations or inconclusive:
        raise MonthlyCloseRolloutError(
            "write_control_remediation_open",
            "仍有歧义或补救事项未收敛，不能切换控制权",
            detail={
                "inconclusive_verifications": inconclusive,
                "open_remediations": open_remediations,
            },
        )
    undrained = int(
        await db.scalar(
            select(func.count(MonthlyCloseOutbox.outbox_id)).where(
                MonthlyCloseOutbox.cycle_id == cycle_id,
                MonthlyCloseOutbox.status.in_(UNDRAINED_OUTBOX_STATES),
            )
        )
        or 0
    )
    if undrained:
        raise MonthlyCloseRolloutError(
            "write_control_outbox_not_drained",
            "月结消息队列尚未排空，不能切换控制权",
            detail={"blocking_count": undrained},
        )


async def switch_write_control(
    db: AsyncSession,
    cycle_id: str,
    expected_version: int,
    owner: Literal["legacy", "assistant"],
    actor: Any,
) -> WriteControlResult:
    """Compare-and-swap one cycle's write owner with durable audit evidence."""
    if owner not in WRITE_CONTROL_OWNERS or expected_version < 1:
        raise MonthlyCloseRolloutError(
            "write_control_request_invalid", "写入控制参数无效"
        )
    current_admin = await _current_admin(db, actor)
    cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if cycle is None:
        raise MonthlyCloseRolloutError("cycle_not_found", "月结周期不存在")
    if cycle.control_version != expected_version:
        replay = await _switch_replay(
            db,
            cycle=cycle,
            expected_version=expected_version,
            owner=owner,
            actor_id=current_admin.user_id,
        )
        if replay is not None:
            return replay
        raise MonthlyCloseRolloutError(
            "control_version_stale",
            "月结写入控制版本已变化，请刷新后重试",
            detail={
                "actual_version": cycle.control_version,
                "expected_version": expected_version,
            },
        )
    if cycle.write_control_owner == owner:
        raise MonthlyCloseRolloutError(
            "write_control_noop", "当前控制方已经是目标值"
        )
    await _assert_switch_boundary_drained(db, cycle.cycle_id)
    if owner == "assistant":
        readiness_errors = _assistant_readiness_errors()
        if not await _schema_is_current(db):
            readiness_errors.append("ALEMBIC_HEAD")
        if readiness_errors:
            raise MonthlyCloseRolloutError(
                "assistant_cutover_not_ready",
                "月结助理写入条件未满足，不能切换控制权",
                detail={"missing_capabilities": readiness_errors},
            )

    previous_owner = cycle.write_control_owner
    new_version = cycle.control_version + 1
    dedupe_key = _switch_dedupe_key(
        cycle.cycle_id, expected_version, owner, current_admin.user_id
    )
    sequence = int(
        await db.scalar(
            select(func.coalesce(func.max(MonthlyCloseEvent.sequence), 0)).where(
                MonthlyCloseEvent.cycle_id == cycle.cycle_id
            )
        )
        or 0
    ) + 1
    now = datetime.now(timezone.utc)
    cycle.write_control_owner = owner
    cycle.control_version = new_version
    db.add(
        MonthlyCloseEvent(
            event_id="MCE-" + _stable_digest(dedupe_key)[:20].upper(),
            cycle_id=cycle.cycle_id,
            sequence=sequence,
            event_type="write_control.switched",
            schema_version="monthly-close-write-control-v1",
            payload_redacted={
                "from_owner": previous_owner,
                "to_owner": owner,
                "from_version": expected_version,
                "to_version": new_version,
            },
            visibility_scope="finance",
            dedupe_key=dedupe_key,
            occurred_at=now,
        )
    )
    await log_action_tx(
        db,
        current_admin.user_id,
        "monthly_close.write_control.switch",
        "monthly_close",
        cycle.cycle_id,
        before_data={
            "owner": previous_owner,
            "control_version": expected_version,
        },
        after_data={"owner": owner, "control_version": new_version},
    )
    try:
        await db.commit()
    except IntegrityError:
        await db.rollback()
        winner = await db.scalar(
            select(MonthlyCloseCycle)
            .where(MonthlyCloseCycle.cycle_id == cycle_id)
            .execution_options(populate_existing=True)
        )
        if winner is not None:
            replay = await _switch_replay(
                db,
                cycle=winner,
                expected_version=expected_version,
                owner=owner,
                actor_id=current_admin.user_id,
            )
            if replay is not None:
                return replay
        raise MonthlyCloseRolloutError(
            "control_version_stale", "月结写入控制版本已被其他请求更新"
        )
    await db.refresh(cycle)
    return WriteControlResult(
        cycle_id=cycle.cycle_id,
        owner=owner,
        control_version=new_version,
        replayed=False,
    )


def _legacy_event_spec(
    cycle: MonthlyCloseCycle,
    *,
    subject_type: str,
    subject_id: str,
    payload: dict[str, Any],
) -> tuple[str, dict[str, Any]]:
    dedupe = f"legacy-history:v1:{subject_type}:{_stable_digest({'cycle_id': cycle.cycle_id, 'subject_id': subject_id})}"
    return dedupe, {
        "billing_month": cycle.billing_month,
        "historical": True,
        "read_only": True,
        "subject_id": subject_id,
        "subject_type": subject_type,
        **payload,
    }


async def migrate_legacy_monthly_close_history(
    db: AsyncSession, cycle_id: str
) -> LegacyMigrationResult:
    """Project deterministic legacy references without changing domain rows."""
    cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if cycle is None:
        raise MonthlyCloseRolloutError("cycle_not_found", "月结周期不存在")
    documents = list(
        await db.scalars(
            select(MonthlyCloseDocument)
            .where(MonthlyCloseDocument.cycle_id == cycle_id)
            .order_by(MonthlyCloseDocument.document_id)
        )
    )
    confirmations = list(
        await db.scalars(
            select(MonthlyCloseStepConfirmation)
            .where(MonthlyCloseStepConfirmation.cycle_id == cycle_id)
            .order_by(MonthlyCloseStepConfirmation.confirmation_id)
        )
    )
    batches = list(
        await db.scalars(
            select(ReconBatch)
            .where(ReconBatch.bill_month == cycle.billing_month)
            .order_by(ReconBatch.batch_id)
        )
    )
    linked_batches = {
        document.engine_id
        for document in documents
        if document.engine_type == "billing_recon" and document.engine_id
    }
    specs = [
        _legacy_event_spec(
            cycle,
            subject_type="cycle",
            subject_id=cycle.cycle_id,
            payload={
                "completed_at": (
                    cycle.completed_at.isoformat() if cycle.completed_at else None
                ),
                "final_snapshot_hash": (
                    _stable_digest(cycle.final_snapshot)
                    if cycle.final_snapshot is not None
                    else None
                ),
                "status": cycle.status,
            },
        )
    ]
    specs.extend(
        _legacy_event_spec(
            cycle,
            subject_type="document",
            subject_id=document.document_id,
            payload={
                "archived": not document.is_active,
                "source_type": document.source_type,
                "status": document.processing_status,
            },
        )
        for document in documents
    )
    specs.extend(
        _legacy_event_spec(
            cycle,
            subject_type="batch",
            subject_id=batch.batch_id,
            payload={
                "mapping_status": (
                    "mapped" if batch.batch_id in linked_batches else "unmapped"
                ),
                "status": batch.status,
            },
        )
        for batch in batches
    )
    specs.extend(
        _legacy_event_spec(
            cycle,
            subject_type="confirmation",
            subject_id=confirmation.confirmation_id,
            payload={"step_key": confirmation.step_key},
        )
        for confirmation in confirmations
    )
    existing_keys = set(
        await db.scalars(
            select(MonthlyCloseEvent.dedupe_key).where(
                MonthlyCloseEvent.cycle_id == cycle_id,
                MonthlyCloseEvent.dedupe_key.in_([item[0] for item in specs]),
            )
        )
    )
    sequence = int(
        await db.scalar(
            select(func.coalesce(func.max(MonthlyCloseEvent.sequence), 0)).where(
                MonthlyCloseEvent.cycle_id == cycle_id
            )
        )
        or 0
    )
    created = 0
    now = datetime.now(timezone.utc)
    for dedupe_key, payload in specs:
        if dedupe_key in existing_keys:
            continue
        sequence += 1
        db.add(
            MonthlyCloseEvent(
                event_id="MCE-" + _stable_digest(dedupe_key)[:20].upper(),
                cycle_id=cycle_id,
                sequence=sequence,
                event_type="legacy.history_projected",
                schema_version="monthly-close-legacy-history-v1",
                payload_redacted=payload,
                visibility_scope="finance",
                dedupe_key=dedupe_key,
                occurred_at=now,
            )
        )
        created += 1
    await db.commit()
    return LegacyMigrationResult(
        cycle_id=cycle_id,
        created_events=created,
        existing_events=len(specs) - created,
    )


async def legacy_history_view(db: AsyncSession, cycle_id: str) -> dict[str, Any]:
    """Permission-layer building block for old pages; every row is read-only."""
    cycle = await db.get(MonthlyCloseCycle, cycle_id)
    if cycle is None:
        raise MonthlyCloseRolloutError("cycle_not_found", "月结周期不存在")
    documents = list(
        await db.scalars(
            select(MonthlyCloseDocument)
            .where(MonthlyCloseDocument.cycle_id == cycle_id)
            .order_by(MonthlyCloseDocument.document_id)
        )
    )
    batches = list(
        await db.scalars(
            select(ReconBatch)
            .where(ReconBatch.bill_month == cycle.billing_month)
            .order_by(ReconBatch.batch_id)
        )
    )
    linked_batches = {
        item.engine_id
        for item in documents
        if item.engine_type == "billing_recon" and item.engine_id
    }
    events = list(
        await db.scalars(
            select(MonthlyCloseEvent)
            .where(
                MonthlyCloseEvent.cycle_id == cycle_id,
                MonthlyCloseEvent.event_type.in_(
                    ("legacy.history_projected", "write_control.switched")
                ),
            )
            .order_by(MonthlyCloseEvent.sequence)
        )
    )
    audits = list(
        await db.scalars(
            select(AuditLog)
            .where(
                AuditLog.resource_type == "monthly_close",
                AuditLog.resource_id == cycle_id,
            )
            .order_by(AuditLog.log_id)
        )
    )
    return {
        "cycle": {
            "cycle_id": cycle.cycle_id,
            "billing_month": cycle.billing_month,
            "status": cycle.status,
            "completed_at": (
                cycle.completed_at.isoformat() if cycle.completed_at else None
            ),
            "final_snapshot": cycle.final_snapshot,
            "write_control_owner": cycle.write_control_owner,
            "control_version": cycle.control_version,
            "read_only": True,
        },
        "documents": [
            {
                "document_id": item.document_id,
                "source_type": item.source_type,
                "status": item.processing_status,
                "archived": not item.is_active,
                "read_only": True,
            }
            for item in documents
        ],
        "batches": [
            {
                "batch_id": item.batch_id,
                "status": item.status,
                "mapping_status": (
                    "mapped" if item.batch_id in linked_batches else "unmapped"
                ),
                "label": (
                    "历史记录"
                    if item.batch_id in linked_batches
                    else "历史记录，仅供查看"
                ),
                "blocks_current_cycle": False,
                "read_only": True,
            }
            for item in batches
        ],
        "events": [
            {
                "event_id": item.event_id,
                "event_type": item.event_type,
                "payload": item.payload_redacted,
                "read_only": True,
            }
            for item in events
        ],
        "audits": [
            {
                "action": item.action,
                "created_at": item.created_at.isoformat() if item.created_at else None,
                "read_only": True,
            }
            for item in audits
        ],
    }
