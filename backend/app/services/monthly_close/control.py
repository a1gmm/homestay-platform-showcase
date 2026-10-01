"""Deterministic proposal, approval, and execution control plane.

The assistant may prepare canonical data, but this module never interprets that
data as a generic database patch.  A command can mutate business state only
through an explicitly registered, typed handler.  Handlers run inside the
attempt transaction and receive the stable business idempotency key unchanged.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
import hashlib
import json
import re
from typing import Any, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from sqlalchemy import func, inspect as sa_inspect, select
from sqlalchemy.exc import NoInspectionAvailable
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.monthly_close import MonthlyCloseCycle
from app.models.monthly_close_control import (
    MonthlyCloseApproval,
    MonthlyCloseApprovalEvaluation,
    MonthlyCloseExecutionAttempt,
    MonthlyCloseIssueInstance,
    MonthlyCloseProposal,
    MonthlyCloseRemediation,
    MonthlyCloseVerification,
)
from app.models.audit_log import AuditLog
from app.models.user import User
from app.services.audit import log_action_tx


PROPOSAL_TYPES = frozenset(
    {
        "ota_reconciliation",
        "ota_appeal_adjudication",
        "service_fee_reconciliation",
        "utility_reconciliation",
        "operating_expense_import",
        "issue_resolution",
        "generate_owner_settlements",
        "finalize_monthly_close",
    }
)
APPROVAL_POLICY = {
    "approval_roles": ["admin"],
    "maker_checker_required": False,
    "policy_version": "monthly-close-approval-v1",
    "required_approvals": 1,
}
HASH_RE = re.compile(r"[0-9a-f]{64}")
SAFE_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,159}")
UNTRUSTED_PROPOSAL_BINDING = "0" * 64
TERMINAL_ATTEMPT_STATUSES = frozenset(
    {"verified", "failed_safe", "failed_confirmed", "remediation_required"}
)


class MonthlyCloseControlError(RuntimeError):
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


class CommitOutcomeUnknown(RuntimeError):
    """The business commit may have succeeded but its acknowledgement was lost."""


@dataclass(frozen=True)
class ProposalFreshnessContext:
    evidence_hash: str
    subject_versions: dict[str, Any]
    active_input_set_hash: str
    mapping_versions: dict[str, Any]
    ruleset_version: str
    calculation_version: str
    configuration_snapshot_hash: str
    control_version: int

    def __post_init__(self) -> None:
        for value, label in (
            (self.evidence_hash, "evidence_hash"),
            (self.active_input_set_hash, "active_input_set_hash"),
            (self.configuration_snapshot_hash, "configuration_snapshot_hash"),
        ):
            if not isinstance(value, str) or HASH_RE.fullmatch(value) is None:
                raise ValueError(f"{label} must be a lowercase SHA-256 hex digest")
        if self.control_version < 1:
            raise ValueError("control_version must be positive")


class CanonicalCommand(BaseModel):
    """Non-executable command data accepted by the control plane."""

    model_config = ConfigDict(extra="forbid")

    command_type: str
    subject_id: str
    before: dict[str, Any]
    after: dict[str, Any]
    amount_impact: Decimal
    evidence_refs: list[dict[str, Any]]
    business_idempotency_key: str

    @field_validator("command_type")
    @classmethod
    def validate_command_type(cls, value: str) -> str:
        if value not in PROPOSAL_TYPES:
            raise ValueError("command type is not allowlisted")
        return value

    @field_validator("subject_id", "business_idempotency_key")
    @classmethod
    def validate_safe_identifier(cls, value: str) -> str:
        if not isinstance(value, str) or SAFE_ID_RE.fullmatch(value) is None:
            raise ValueError("command identifier is invalid")
        return value

    @field_validator("amount_impact", mode="before")
    @classmethod
    def validate_money(cls, value: Any) -> Decimal:
        if isinstance(value, float):
            raise ValueError("floating point money is not accepted")
        try:
            return Decimal(str(value)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        except (InvalidOperation, TypeError) as exc:
            raise ValueError("amount impact is invalid") from exc


ExecuteHandler = Callable[
    [AsyncSession, MonthlyCloseCycle, MonthlyCloseProposal, dict[str, Any], User, str],
    Awaitable[dict[str, Any]],
]
VerifyHandler = Callable[
    [
        AsyncSession,
        MonthlyCloseCycle,
        MonthlyCloseProposal,
        dict[str, Any],
        MonthlyCloseExecutionAttempt,
    ],
    Awaitable[dict[str, Any]],
]
ContextLoader = Callable[
    [AsyncSession, MonthlyCloseCycle, Sequence[dict[str, Any]]],
    Awaitable[ProposalFreshnessContext],
]
ExecutionAuditIdentity = Callable[[dict[str, Any]], Mapping[str, Any]]


@dataclass(frozen=True)
class RegisteredCommandHandler:
    execute: ExecuteHandler
    verify: VerifyHandler
    load_context: ContextLoader | None = None


class CommandHandlerRegistry:
    """Explicit allowlist; absence is a safe failure, never a generic patch."""

    def __init__(self) -> None:
        self._handlers: dict[str, RegisteredCommandHandler] = {}

    def register(
        self,
        command_type: str,
        *,
        execute: ExecuteHandler,
        verify: VerifyHandler,
        load_context: ContextLoader | None = None,
        replace: bool = False,
    ) -> None:
        if command_type not in PROPOSAL_TYPES:
            raise ValueError("command type is not allowlisted")
        if command_type in self._handlers and not replace:
            raise ValueError(f"handler already registered for {command_type}")
        self._handlers[command_type] = RegisteredCommandHandler(
            execute=execute,
            verify=verify,
            load_context=load_context,
        )

    def get(self, command_type: str) -> RegisteredCommandHandler | None:
        return self._handlers.get(command_type)

    def as_mapping(self) -> Mapping[str, RegisteredCommandHandler]:
        return dict(self._handlers)


_DEFAULT_HANDLERS = CommandHandlerRegistry()
_DEFAULTS_READY = False


def _default_handler_registry() -> CommandHandlerRegistry:
    global _DEFAULTS_READY
    if not _DEFAULTS_READY:
        from app.services.monthly_close.adapters import (
            execute_operating_expense_command,
            execute_service_command,
            execute_utility_command,
            execute_ota_appeal_adjudication_command,
            execute_ota_command,
            load_operating_expense_context,
            load_service_context,
            load_utility_context,
            load_ota_appeal_adjudication_context,
            load_ota_context,
            verify_operating_expense_command,
            verify_service_command,
            verify_utility_command,
            verify_ota_appeal_adjudication_command,
            verify_ota_command,
        )
        from app.services.monthly_close.finalization import (
            execute_settlement_command,
            execute_finalize_command,
            load_settlement_context,
            load_finalize_context,
            verify_settlement_command,
            verify_finalize_command,
        )

        _DEFAULT_HANDLERS.register(
            "ota_reconciliation",
            execute=execute_ota_command,
            verify=verify_ota_command,
            load_context=load_ota_context,
        )
        _DEFAULT_HANDLERS.register(
            "ota_appeal_adjudication",
            execute=execute_ota_appeal_adjudication_command,
            verify=verify_ota_appeal_adjudication_command,
            load_context=load_ota_appeal_adjudication_context,
        )
        _DEFAULT_HANDLERS.register(
            "service_fee_reconciliation",
            execute=execute_service_command,
            verify=verify_service_command,
            load_context=load_service_context,
        )
        _DEFAULT_HANDLERS.register(
            "utility_reconciliation",
            execute=execute_utility_command,
            verify=verify_utility_command,
            load_context=load_utility_context,
        )
        _DEFAULT_HANDLERS.register(
            "operating_expense_import",
            execute=execute_operating_expense_command,
            verify=verify_operating_expense_command,
            load_context=load_operating_expense_context,
        )
        _DEFAULT_HANDLERS.register(
            "generate_owner_settlements",
            execute=execute_settlement_command,
            verify=verify_settlement_command,
            load_context=load_settlement_context,
        )
        _DEFAULT_HANDLERS.register(
            "finalize_monthly_close",
            execute=execute_finalize_command,
            verify=verify_finalize_command,
            load_context=load_finalize_context,
        )
        _DEFAULTS_READY = True
    return _DEFAULT_HANDLERS


def register_command_handler(
    command_type: str,
    *,
    execute: ExecuteHandler,
    verify: VerifyHandler,
    load_context: ContextLoader | None = None,
) -> None:
    """Register one production adapter during application startup."""
    _default_handler_registry().register(
        command_type,
        execute=execute,
        verify=verify,
        load_context=load_context,
    )


def get_command_handler_registry() -> CommandHandlerRegistry:
    return _default_handler_registry()


def _identifier(prefix: str, body_length: int) -> str:
    return prefix + uuid4().hex[:body_length].upper()


def _actor_id(actor: Any) -> str:
    if isinstance(actor, dict):
        value = actor.get("user_id")
    else:
        try:
            identity = sa_inspect(actor).identity
        except NoInspectionAvailable:
            identity = None
        value = identity[0] if identity else getattr(actor, "user_id", None)
    if not isinstance(value, str) or not value:
        raise MonthlyCloseControlError("actor_invalid", "当前操作人无效")
    return value


async def _current_user(
    db: AsyncSession,
    actor: Any,
    *,
    allowed_roles: frozenset[str],
    forbidden_code: str,
) -> User:
    actor_id = _actor_id(actor)
    user = await db.scalar(
        select(User)
        .where(User.user_id == actor_id)
        .execution_options(populate_existing=True)
    )
    role = user.role.value if user is not None else None
    if user is None or not user.is_active or role not in allowed_roles:
        raise MonthlyCloseControlError(forbidden_code, "当前账号无权执行此操作")
    return user


def _validate_request_id(request_id: str) -> str:
    value = (request_id or "").strip()
    if SAFE_ID_RE.fullmatch(value) is None or len(value) > 80:
        raise MonthlyCloseControlError("request_id_invalid", "请求编号无效")
    return value


def validate_request_id(request_id: str) -> str:
    """Validate a caller-supplied idempotency request identifier."""
    return _validate_request_id(request_id)


def _canonical_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        raise MonthlyCloseControlError(
            "canonical_value_invalid", "方案不接受浮点数，请使用精确金额字符串"
        )
    if isinstance(value, Decimal):
        normalized = value.normalize()
        return format(normalized, "f") if normalized != 0 else "0"
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, dict):
        return {
            str(key): _canonical_value(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
        }
    if isinstance(value, (list, tuple)):
        return [_canonical_value(item) for item in value]
    raise MonthlyCloseControlError(
        "canonical_value_invalid", "方案包含不支持的数据类型"
    )


def _canonical_json(value: Any) -> str:
    return json.dumps(
        _canonical_value(value),
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _canonical_commands(commands: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    if not commands:
        raise MonthlyCloseControlError("commands_required", "方案至少需要一条受控命令")
    parsed: list[dict[str, Any]] = []
    keys: set[str] = set()
    for raw in commands:
        try:
            command = CanonicalCommand.model_validate(raw)
        except ValidationError as exc:
            raise MonthlyCloseControlError(
                "command_invalid", "方案命令格式不符合白名单契约"
            ) from exc
        key = command.business_idempotency_key
        if key in keys:
            raise MonthlyCloseControlError(
                "business_idempotency_key_duplicate", "方案内命令业务键重复"
            )
        keys.add(key)
        item = command.model_dump(mode="python")
        item["amount_impact"] = format(command.amount_impact, ".2f")
        item["before"] = _canonical_value(item["before"])
        item["after"] = _canonical_value(item["after"])
        item["evidence_refs"] = sorted(
            (_canonical_value(ref) for ref in item["evidence_refs"]),
            key=_canonical_json,
        )
        parsed.append(_canonical_value(item))
    return sorted(parsed, key=lambda item: item["business_idempotency_key"])


def _binding_datetime(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _proposal_binding_material(proposal: MonthlyCloseProposal) -> dict[str, Any]:
    """Every immutable proposal input; lifecycle status is deliberately absent."""
    return {
        "active_input_set_hash": proposal.active_input_set_hash,
        "approval_policy_snapshot": proposal.approval_policy_snapshot,
        "calculation_version": proposal.calculation_version,
        "canonical_payload": proposal.canonical_payload,
        "configuration_snapshot_hash": proposal.configuration_snapshot_hash,
        "created_at": _binding_datetime(proposal.created_at),
        "created_by_run_id": proposal.created_by_run_id,
        "cycle_id": proposal.cycle_id,
        "evidence_hash": proposal.evidence_hash,
        "evidence_refs": proposal.evidence_refs,
        "expires_at": _binding_datetime(proposal.expires_at),
        "impact_amount": proposal.impact_amount,
        "impact_snapshot": proposal.impact_snapshot,
        "mapping_versions": proposal.mapping_versions,
        "payload_hash": proposal.payload_hash,
        "proposal_id": proposal.proposal_id,
        "proposal_type": proposal.proposal_type,
        "proposal_version": proposal.proposal_version,
        "ruleset_version": proposal.ruleset_version,
        "schema_version": proposal.schema_version,
        "subject_versions": proposal.subject_versions,
        "supersedes_proposal_id": proposal.supersedes_proposal_id,
        "validation_snapshot": proposal.validation_snapshot,
    }


def _proposal_binding_digest(proposal: MonthlyCloseProposal) -> str:
    return _sha256(_proposal_binding_material(proposal))


def _context_from_proposal(
    cycle: MonthlyCloseCycle, proposal: MonthlyCloseProposal
) -> ProposalFreshnessContext:
    return ProposalFreshnessContext(
        evidence_hash=proposal.evidence_hash,
        subject_versions=dict(proposal.subject_versions),
        active_input_set_hash=proposal.active_input_set_hash,
        mapping_versions=dict(proposal.mapping_versions),
        ruleset_version=proposal.ruleset_version,
        calculation_version=proposal.calculation_version,
        configuration_snapshot_hash=proposal.configuration_snapshot_hash,
        control_version=cycle.control_version,
    )


async def _load_registered_context(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    commands: Sequence[dict[str, Any]],
    registry: CommandHandlerRegistry,
) -> ProposalFreshnessContext | None:
    loaded: ProposalFreshnessContext | None = None
    for command_type in sorted({item["command_type"] for item in commands}):
        handler = registry.get(command_type)
        if handler is None or handler.load_context is None:
            return None
        current = await handler.load_context(db, cycle, commands)
        if loaded is not None and current != loaded:
            raise MonthlyCloseControlError(
                "proposal_context_conflict",
                "同一方案的命令不属于同一个原子上下文",
            )
        loaded = current
    return loaded


def _contexts_equal(
    proposal: MonthlyCloseProposal, current: ProposalFreshnessContext
) -> bool:
    return (
        proposal.evidence_hash == current.evidence_hash
        and proposal.subject_versions == current.subject_versions
        and proposal.active_input_set_hash == current.active_input_set_hash
        and proposal.mapping_versions == current.mapping_versions
        and proposal.ruleset_version == current.ruleset_version
        and proposal.calculation_version == current.calculation_version
        and proposal.configuration_snapshot_hash
        == current.configuration_snapshot_hash
        and proposal.validation_snapshot.get("control_version")
        == current.control_version
    )


def _payload_is_immutable(proposal: MonthlyCloseProposal) -> bool:
    expected_payload_hash = _sha256(proposal.canonical_payload)
    return (
        proposal.proposal_binding_hash != UNTRUSTED_PROPOSAL_BINDING
        and HASH_RE.fullmatch(proposal.proposal_binding_hash or "") is not None
        and expected_payload_hash == proposal.payload_hash
        and proposal.proposal_binding_hash == _proposal_binding_digest(proposal)
    )


def proposal_binding_is_valid(proposal: MonthlyCloseProposal) -> bool:
    """Return whether the persisted immutable proposal digest is authoritative."""
    return _payload_is_immutable(proposal)


async def _proposal_is_current(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    proposal: MonthlyCloseProposal,
    registry: CommandHandlerRegistry,
) -> bool:
    if proposal.status in {"stale", "superseded", "rejected"}:
        return False
    expires_at = proposal.expires_at
    if expires_at is not None:
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
        if expires_at <= datetime.now(timezone.utc):
            return False
    if not _payload_is_immutable(proposal):
        return False
    commands = list(proposal.canonical_payload.get("commands") or [])
    current = await _load_registered_context(db, cycle, commands, registry)
    if current is None:
        current = _context_from_proposal(cycle, proposal)
    return _contexts_equal(proposal, current)


async def _mark_stale_and_raise(
    db: AsyncSession,
    proposal: MonthlyCloseProposal,
    actor_id: str,
) -> None:
    if proposal.status not in {"stale", "superseded", "rejected"}:
        proposal.status = "stale"
        await log_action_tx(
            db,
            actor_id,
            "monthly_close.proposal.stale",
            "monthly_close_proposal",
            proposal.proposal_id,
            after_data={"cycle_id": proposal.cycle_id, "proposal_id": proposal.proposal_id},
        )
        await db.commit()
    raise MonthlyCloseControlError(
        "proposal_stale", "方案依据已经变化，请重新生成并审批"
    )


async def create_proposal(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    actor: Any,
    commands: Sequence[dict[str, Any]],
    *,
    evidence_hash: str,
    request_id: str | None = None,
    proposal_type: str | None = None,
    evidence_refs: Sequence[dict[str, Any]] | None = None,
    subject_versions: dict[str, Any] | None = None,
    active_input_set_hash: str | None = None,
    mapping_versions: dict[str, Any] | None = None,
    ruleset_version: str = "monthly-close-rules-v1",
    calculation_version: str = "monthly-close-calculation-v1",
    configuration_snapshot_hash: str | None = None,
    impact_snapshot: dict[str, Any] | None = None,
    validation_snapshot: dict[str, Any] | None = None,
    expires_at: datetime | None = None,
    created_by_run_id: str | None = None,
    handler_registry: CommandHandlerRegistry | None = None,
    server_owned_commands: bool = False,
    semantic_source_submission: bool = False,
) -> MonthlyCloseProposal:
    current_actor = await _current_user(
        db,
        actor,
        allowed_roles=frozenset({"admin", "finance", "operator"}),
        forbidden_code="proposal_forbidden",
    )
    client_request_id = request_id
    if client_request_id is not None:
        client_request_id = _validate_request_id(client_request_id)
    if HASH_RE.fullmatch(evidence_hash or "") is None:
        raise MonthlyCloseControlError("evidence_hash_invalid", "证据哈希无效")
    canonical_commands = _canonical_commands(commands)
    command_types = {item["command_type"] for item in canonical_commands}
    if command_types.intersection(
        {"ota_reconciliation", "ota_appeal_adjudication"}
    ) and not server_owned_commands:
        raise MonthlyCloseControlError(
            "ota_dedicated_builder_required",
            "OTA方案必须由专用入口根据当前账单事实生成",
        )
    inferred_type = next(iter(command_types)) if len(command_types) == 1 else None
    selected_type = proposal_type or inferred_type
    if selected_type not in PROPOSAL_TYPES or selected_type not in command_types:
        raise MonthlyCloseControlError(
            "proposal_type_invalid", "方案类型必须与受控命令一致"
        )
    registry = handler_registry or _default_handler_registry()
    locked_cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle.cycle_id)
        .with_for_update()
    )
    if locked_cycle is None:
        raise MonthlyCloseControlError("cycle_not_found", "月结周期不存在")
    if locked_cycle.status == "completed":
        raise MonthlyCloseControlError("cycle_completed", "已完成月结不能创建写入方案")

    registered_context = await _load_registered_context(
        db, locked_cycle, canonical_commands, registry
    )
    fallback_context = ProposalFreshnessContext(
        evidence_hash=evidence_hash,
        subject_versions=subject_versions
        or {locked_cycle.cycle_id: locked_cycle.control_version},
        active_input_set_hash=active_input_set_hash or evidence_hash,
        mapping_versions=mapping_versions or {},
        ruleset_version=ruleset_version,
        calculation_version=calculation_version,
        configuration_snapshot_hash=configuration_snapshot_hash
        or _sha256(
            {
                "billing_month": locked_cycle.billing_month,
                "cycle_id": locked_cycle.cycle_id,
            }
        ),
        control_version=locked_cycle.control_version,
    )
    context = registered_context or fallback_context
    if evidence_hash != context.evidence_hash:
        raise MonthlyCloseControlError(
            "proposal_evidence_changed", "方案证据已经变化，请重新计算"
        )
    request_id = client_request_id
    if semantic_source_submission:
        if client_request_id is None:
            raise MonthlyCloseControlError(
                "request_id_required", "来源方案必须提供提交编号"
            )
        request_id = (
            f"source:{selected_type}:v1:"
            f"{_sha256({'client_request_id': client_request_id, 'evidence_hash': context.evidence_hash})[:32]}"
        )

    canonical_payload = {
        "commands": canonical_commands,
        "creator": {"actor_id": current_actor.user_id, "actor_type": "user"},
        "schema_version": "v1",
    }
    payload_hash = _sha256(canonical_payload)
    existing_rows = list(
        await db.scalars(
            select(MonthlyCloseProposal).where(
                MonthlyCloseProposal.cycle_id == locked_cycle.cycle_id
            )
        )
    )
    if request_id is not None:
        existing = next(
            (
                row
                for row in existing_rows
                if (row.validation_snapshot or {}).get("create_request_id")
                == request_id
            ),
            None,
        )
        if existing is not None:
            if (
                existing.payload_hash == payload_hash
                and existing.evidence_hash == context.evidence_hash
            ):
                return existing
            raise MonthlyCloseControlError(
                "request_id_conflict", "同一请求编号不能创建不同方案"
            )

    latest_version = max(
        (
            row.proposal_version
            for row in existing_rows
            if row.proposal_type == selected_type
        ),
        default=0,
    )
    supersedes_proposal: MonthlyCloseProposal | None = None
    if semantic_source_submission:
        latest = max(
            (row for row in existing_rows if row.proposal_type == selected_type),
            key=lambda row: (row.proposal_version, row.proposal_id),
            default=None,
        )
        if latest is not None and (
            latest.status in {"rejected", "stale", "superseded"}
            or latest.evidence_hash != context.evidence_hash
        ):
            supersedes_proposal = latest
            if (
                latest.evidence_hash != context.evidence_hash
                and latest.status not in {"rejected", "stale", "superseded"}
            ):
                latest.status = "superseded"
    flattened_refs = list(evidence_refs or []) or [
        ref for command in canonical_commands for ref in command["evidence_refs"]
    ]
    amount = sum(
        (Decimal(command["amount_impact"]) for command in canonical_commands),
        Decimal("0.00"),
    )
    immutable_validation = {
        **_canonical_value(validation_snapshot or {"valid": True}),
        "control_version": context.control_version,
        "create_request_id": request_id,
        "created_by_user_id": current_actor.user_id,
        **(
            {
                "client_request_id": client_request_id,
                "source_submission_semantics": "v1",
            }
            if semantic_source_submission
            else {}
        ),
    }
    proposal_id = _identifier("MCP-", 20)
    created_at = datetime.now(timezone.utc)
    proposal = MonthlyCloseProposal(
        proposal_id=proposal_id,
        cycle_id=locked_cycle.cycle_id,
        proposal_type=selected_type,
        status="pending_approval",
        proposal_version=latest_version + 1,
        schema_version="v1",
        canonical_payload=canonical_payload,
        payload_hash=payload_hash,
        proposal_binding_hash=UNTRUSTED_PROPOSAL_BINDING,
        evidence_refs=sorted(
            (_canonical_value(ref) for ref in flattened_refs), key=_canonical_json
        ),
        evidence_hash=context.evidence_hash,
        subject_versions=_canonical_value(context.subject_versions),
        active_input_set_hash=context.active_input_set_hash,
        mapping_versions=_canonical_value(context.mapping_versions),
        ruleset_version=context.ruleset_version,
        calculation_version=context.calculation_version,
        configuration_snapshot_hash=context.configuration_snapshot_hash,
        approval_policy_snapshot=dict(APPROVAL_POLICY),
        impact_snapshot=_canonical_value(
            impact_snapshot
            or {
                "command_count": len(canonical_commands),
                "total_amount": format(amount, ".2f"),
            }
        ),
        impact_amount=amount,
        validation_snapshot=immutable_validation,
        expires_at=expires_at,
        created_by_run_id=created_by_run_id,
        supersedes_proposal_id=(
            supersedes_proposal.proposal_id if supersedes_proposal else None
        ),
        created_at=created_at,
    )
    proposal.proposal_binding_hash = _proposal_binding_digest(proposal)
    db.add(proposal)
    await log_action_tx(
        db,
        current_actor.user_id,
        "monthly_close.proposal.create",
        "monthly_close_proposal",
        proposal.proposal_id,
        after_data={
            "cycle_id": locked_cycle.cycle_id,
            "proposal_id": proposal.proposal_id,
            "proposal_type": selected_type,
            "payload_hash": payload_hash,
            "evidence_hash": context.evidence_hash,
            "request_id": request_id,
        },
    )
    await db.commit()
    await db.refresh(proposal)
    return proposal


def _approval_set_hash(
    proposal: MonthlyCloseProposal, approvals: Sequence[MonthlyCloseApproval]
) -> str:
    return _sha256(
        {
            "approval_ids": [item.approval_id for item in approvals],
            "approvals": [
                {
                    "approval_id": item.approval_id,
                    "decided_by": item.decided_by,
                    "decision": item.decision,
                    "maker_is_checker": item.maker_is_checker,
                    "payload_hash": item.payload_hash,
                    "policy_version": item.policy_version,
                    "proposal_binding_hash": item.proposal_binding_hash,
                }
                for item in approvals
            ],
            "evidence_hash": proposal.evidence_hash,
            "policy_snapshot": proposal.approval_policy_snapshot,
            "proposal_binding_hash": proposal.proposal_binding_hash,
            "proposal_id": proposal.proposal_id,
        }
    )


async def _locked_proposal(
    db: AsyncSession, proposal: MonthlyCloseProposal
) -> tuple[MonthlyCloseCycle, MonthlyCloseProposal]:
    return await _locked_proposal_by_ids(db, proposal.cycle_id, proposal.proposal_id)


async def _locked_proposal_by_ids(
    db: AsyncSession, cycle_id: str, proposal_id: str
) -> tuple[MonthlyCloseCycle, MonthlyCloseProposal]:
    cycle = await db.scalar(
        select(MonthlyCloseCycle)
        .where(MonthlyCloseCycle.cycle_id == cycle_id)
        .with_for_update()
    )
    locked = await db.scalar(
        select(MonthlyCloseProposal)
        .where(MonthlyCloseProposal.proposal_id == proposal_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if cycle is None or locked is None or locked.cycle_id != cycle.cycle_id:
        raise MonthlyCloseControlError("proposal_not_found", "方案不存在")
    return cycle, locked


async def approve_proposal(
    db: AsyncSession,
    proposal: MonthlyCloseProposal,
    actor: Any,
    *,
    request_id: str,
    reason: str | None = None,
    handler_registry: CommandHandlerRegistry | None = None,
) -> MonthlyCloseApproval:
    current_actor = await _current_user(
        db,
        actor,
        allowed_roles=frozenset({"admin"}),
        forbidden_code="approval_forbidden",
    )
    request_id = _validate_request_id(request_id)
    registry = handler_registry or _default_handler_registry()
    cycle, locked = await _locked_proposal(db, proposal)
    existing_request = await db.scalar(
        select(MonthlyCloseApproval).where(
            MonthlyCloseApproval.proposal_id == locked.proposal_id,
            MonthlyCloseApproval.request_id == request_id,
        )
    )
    if existing_request is not None:
        return existing_request
    if not await _proposal_is_current(db, cycle, locked, registry):
        await _mark_stale_and_raise(db, locked, current_actor.user_id)
    existing_actor = await db.scalar(
        select(MonthlyCloseApproval).where(
            MonthlyCloseApproval.proposal_id == locked.proposal_id,
            MonthlyCloseApproval.decided_by == current_actor.user_id,
        )
    )
    if existing_actor is not None:
        if existing_actor.decision == "approved":
            return existing_actor
        raise MonthlyCloseControlError("proposal_rejected", "该方案已经被拒绝")
    if locked.status == "rejected":
        raise MonthlyCloseControlError("proposal_rejected", "该方案已经被拒绝")

    approved_rows = list(
        await db.scalars(
            select(MonthlyCloseApproval)
            .where(
                MonthlyCloseApproval.proposal_id == locked.proposal_id,
                MonthlyCloseApproval.decision == "approved",
            )
            .order_by(MonthlyCloseApproval.sequence_no, MonthlyCloseApproval.approval_id)
        )
    )
    approval = MonthlyCloseApproval(
        approval_id=_identifier("MCAP-", 19),
        proposal_id=locked.proposal_id,
        payload_hash=locked.payload_hash,
        proposal_binding_hash=locked.proposal_binding_hash,
        decision="approved",
        decided_by=current_actor.user_id,
        approval_role=(
            "final_close_approver"
            if locked.proposal_type == "finalize_monthly_close"
            else "finance_approver"
        ),
        policy_version=locked.approval_policy_snapshot["policy_version"],
        required_approvals=locked.approval_policy_snapshot["required_approvals"],
        sequence_no=len(approved_rows) + 1,
        request_id=request_id,
        maker_is_checker=(
            locked.validation_snapshot.get("created_by_user_id")
            == current_actor.user_id
        ),
        reason=reason,
    )
    db.add(approval)
    await db.flush()
    approvals = [*approved_rows, approval]
    required = int(locked.approval_policy_snapshot["required_approvals"])
    satisfied = len(approvals) >= required
    evaluation = MonthlyCloseApprovalEvaluation(
        evaluation_id=_identifier("MCEV-", 19),
        proposal_id=locked.proposal_id,
        policy_version=locked.approval_policy_snapshot["policy_version"],
        approval_ids=[item.approval_id for item in approvals],
        approval_set_hash=_approval_set_hash(locked, approvals),
        proposal_binding_hash=locked.proposal_binding_hash,
        satisfied=satisfied,
    )
    db.add(evaluation)
    if satisfied:
        locked.status = "approved"
    await log_action_tx(
        db,
        current_actor.user_id,
        "monthly_close.proposal.approve",
        "monthly_close_proposal",
        locked.proposal_id,
        after_data={
            "approval_id": approval.approval_id,
            "approval_set_hash": evaluation.approval_set_hash,
            "evidence_hash": locked.evidence_hash,
            "maker_is_checker": approval.maker_is_checker,
            "payload_hash": locked.payload_hash,
            "policy_version": evaluation.policy_version,
            "request_id": request_id,
        },
    )
    await db.commit()
    await db.refresh(approval)
    return approval


async def reject_proposal(
    db: AsyncSession,
    proposal: MonthlyCloseProposal,
    actor: Any,
    *,
    request_id: str,
    reason: str,
    handler_registry: CommandHandlerRegistry | None = None,
) -> MonthlyCloseApproval:
    current_actor = await _current_user(
        db,
        actor,
        allowed_roles=frozenset({"admin"}),
        forbidden_code="approval_forbidden",
    )
    request_id = _validate_request_id(request_id)
    reason = (reason or "").strip()
    if not reason:
        raise MonthlyCloseControlError("rejection_reason_required", "拒绝方案必须说明原因")
    registry = handler_registry or _default_handler_registry()
    cycle, locked = await _locked_proposal(db, proposal)
    existing = await db.scalar(
        select(MonthlyCloseApproval).where(
            MonthlyCloseApproval.proposal_id == locked.proposal_id,
            MonthlyCloseApproval.request_id == request_id,
        )
    )
    if existing is not None:
        return existing
    if not await _proposal_is_current(db, cycle, locked, registry):
        await _mark_stale_and_raise(db, locked, current_actor.user_id)
    if locked.status == "approved":
        raise MonthlyCloseControlError(
            "proposal_already_approved", "已批准方案不能再用拒绝覆盖"
        )
    decision = MonthlyCloseApproval(
        approval_id=_identifier("MCAP-", 19),
        proposal_id=locked.proposal_id,
        payload_hash=locked.payload_hash,
        proposal_binding_hash=locked.proposal_binding_hash,
        decision="rejected",
        decided_by=current_actor.user_id,
        approval_role=(
            "final_close_approver"
            if locked.proposal_type == "finalize_monthly_close"
            else "finance_approver"
        ),
        policy_version=locked.approval_policy_snapshot["policy_version"],
        required_approvals=locked.approval_policy_snapshot["required_approvals"],
        sequence_no=1,
        request_id=request_id,
        maker_is_checker=(
            locked.validation_snapshot.get("created_by_user_id")
            == current_actor.user_id
        ),
        reason=reason,
    )
    db.add(decision)
    locked.status = "rejected"
    await log_action_tx(
        db,
        current_actor.user_id,
        "monthly_close.proposal.reject",
        "monthly_close_proposal",
        locked.proposal_id,
        after_data={"request_id": request_id, "reason": reason},
    )
    await db.commit()
    await db.refresh(decision)
    return decision


def _attempt_business_key(
    cycle_id: str, commands: Sequence[dict[str, Any]]
) -> str:
    digest = _sha256(
        {
            "business_idempotency_keys": sorted(
                item["business_idempotency_key"] for item in commands
            ),
            "cycle_id": cycle_id,
        }
    )
    return f"business:{digest}"


def execution_lineage_material(
    proposal: MonthlyCloseProposal,
    attempt: MonthlyCloseExecutionAttempt,
) -> dict[str, Any]:
    """Canonical immutable execution facts committed by deterministic verification."""
    commands = list(proposal.canonical_payload.get("commands") or [])
    return {
        "approval_evaluation_id": attempt.approval_evaluation_id,
        "approval_set_hash": attempt.approval_set_hash,
        "attempt_id": attempt.attempt_id,
        "attempt_no": attempt.attempt_no,
        "audit_refs": attempt.audit_refs,
        "command_hashes": [_sha256(command) for command in commands],
        "command_results": attempt.command_results,
        "cycle_id": attempt.cycle_id,
        "idempotency_key": attempt.idempotency_key,
        "proposal_binding_hash": attempt.proposal_binding_hash,
        "proposal_id": attempt.proposal_id,
        "request_id": attempt.request_id,
    }


def execution_lineage_hash(
    proposal: MonthlyCloseProposal,
    attempt: MonthlyCloseExecutionAttempt,
) -> str:
    return _sha256(execution_lineage_material(proposal, attempt))


def verified_execution_evidence_hash(
    *,
    attempt_id: str,
    command_checks: Sequence[dict[str, Any]],
    outcome: str,
    lineage_hash: str,
    verification_request_id: str,
    verified_subject_versions: Mapping[str, Any],
) -> str:
    return _sha256(
        {
            "attempt_id": attempt_id,
            "commands": list(command_checks),
            "execution_lineage_hash": lineage_hash,
            "outcome": outcome,
            "verification_request_id": verification_request_id,
            "verified_subject_versions": dict(verified_subject_versions),
        }
    )


def _expected_verified_subject_versions(
    proposal: MonthlyCloseProposal,
) -> dict[str, Any]:
    versions: dict[str, Any] = {}
    for command in proposal.canonical_payload.get("commands") or []:
        subject_id = command.get("subject_id")
        if not isinstance(subject_id, str):
            return {}
        expected = proposal.subject_versions.get(subject_id)
        if expected is None and subject_id == proposal.cycle_id:
            expected = proposal.subject_versions.get("cycle")
        after = command.get("after") if isinstance(command.get("after"), dict) else {}
        versions[subject_id] = after.get(
            "subject_version", after.get("control_version", expected)
        )
    return versions


async def authoritative_execution_evidence_is_valid(
    db: AsyncSession,
    *,
    proposal: MonthlyCloseProposal,
    attempt: MonthlyCloseExecutionAttempt,
    expected_audit_identity: ExecutionAuditIdentity,
    lock: bool = False,
) -> bool:
    """Validate one executed attempt against its exact handler audit evidence."""

    commands = list(proposal.canonical_payload.get("commands") or [])
    results = (
        attempt.command_results
        if isinstance(attempt.command_results, list)
        else []
    )
    audit_refs = attempt.audit_refs if isinstance(attempt.audit_refs, list) else []
    if (
        attempt.status not in {"succeeded_unverified", "unknown", "verified"}
        or attempt.proposal_id != proposal.proposal_id
        or attempt.cycle_id != proposal.cycle_id
        or attempt.proposal_binding_hash != proposal.proposal_binding_hash
        or SAFE_ID_RE.fullmatch(attempt.request_id or "") is None
        or len(commands) != len(results)
        or not audit_refs
        or not all(isinstance(item, str) for item in audit_refs)
        or audit_refs != sorted(set(audit_refs))
    ):
        return False
    try:
        audit_ids = [int(item) for item in audit_refs]
    except (TypeError, ValueError):
        return False
    statement = select(AuditLog).where(AuditLog.log_id.in_(audit_ids))
    if lock:
        statement = statement.with_for_update()
    audits = list(
        await db.scalars(statement.execution_options(populate_existing=True))
    )
    audits_by_ref = {str(item.log_id): item for item in audits}
    if set(audits_by_ref) != set(audit_refs):
        return False

    result_refs: list[str] = []
    for command, result in zip(commands, results, strict=True):
        if not isinstance(command, dict) or not isinstance(result, dict):
            return False
        identity = _canonical_value(dict(expected_audit_identity(command)))
        if set(identity) != {"action", "resource_type", "resource_id", "result"}:
            return False
        command_id = _sha256(command)
        command_audit_refs = result.get("audit_refs")
        expected_result = {
            "audit_refs": command_audit_refs,
            "business_idempotency_key": command.get("business_idempotency_key"),
            "command_id": command_id,
            "command_type": command.get("command_type"),
            "result": identity["result"],
            "status": "succeeded_unverified",
        }
        if (
            not isinstance(command_audit_refs, list)
            or not command_audit_refs
            or not all(isinstance(item, str) for item in command_audit_refs)
            or command_audit_refs != sorted(set(command_audit_refs))
            or result != expected_result
        ):
            return False
        result_refs.extend(command_audit_refs)
        for audit_ref in command_audit_refs:
            audit = audits_by_ref.get(audit_ref)
            if audit is None:
                return False
            expected_binding = {
                "action": identity["action"],
                "after": command.get("after"),
                "attempt_id": attempt.attempt_id,
                "audit_ref": audit_ref,
                "before": command.get("before"),
                "business_idempotency_key": command.get(
                    "business_idempotency_key"
                ),
                "command_id": command_id,
                "command_type": command.get("command_type"),
                "proposal_id": proposal.proposal_id,
                "request_id": attempt.request_id,
                "resource_id": identity["resource_id"],
                "resource_type": identity["resource_type"],
                "result": identity["result"],
            }
            after_data = audit.after_data if isinstance(audit.after_data, dict) else {}
            if (
                audit.operator_id != attempt.actor_id
                or audit.action != identity["action"]
                or audit.resource_type != identity["resource_type"]
                or audit.resource_id != identity["resource_id"]
                or audit.before_data != command.get("before")
                or after_data.get("approved_after") != command.get("after")
                or after_data.get("business_idempotency_key")
                != command.get("business_idempotency_key")
                or after_data.get("proposal_id") != proposal.proposal_id
                or after_data.get("request_id") != attempt.request_id
                or after_data.get("execution_binding") != expected_binding
            ):
                return False
    return sorted(result_refs) == audit_refs


async def verified_execution_lineage_is_valid(
    db: AsyncSession,
    *,
    proposal_id: str,
    attempt_id: str,
    verification_id: str,
    cycle_id: str,
    proposal_type: str | None = None,
    expected_audit_identity: ExecutionAuditIdentity | None = None,
    lock: bool = False,
) -> bool:
    """Recompute the complete Task 6 proposal-to-verification lineage.

    This is intentionally generic control-plane validation.  OTA callers add
    their document, batch, row, order and economic rereads after this proof.
    """

    def locked(statement):
        return statement.with_for_update() if lock else statement

    proposal = await db.scalar(
        locked(
            select(MonthlyCloseProposal).where(
                MonthlyCloseProposal.proposal_id == proposal_id,
                MonthlyCloseProposal.cycle_id == cycle_id,
            )
        ).execution_options(populate_existing=True)
    )
    attempt = await db.scalar(
        locked(
            select(MonthlyCloseExecutionAttempt).where(
                MonthlyCloseExecutionAttempt.attempt_id == attempt_id,
                MonthlyCloseExecutionAttempt.cycle_id == cycle_id,
                MonthlyCloseExecutionAttempt.proposal_id == proposal_id,
            )
        ).execution_options(populate_existing=True)
    )
    verification = await db.scalar(
        locked(
            select(MonthlyCloseVerification).where(
                MonthlyCloseVerification.verification_id == verification_id,
                MonthlyCloseVerification.cycle_id == cycle_id,
                MonthlyCloseVerification.attempt_id == attempt_id,
            )
        ).execution_options(populate_existing=True)
    )
    if (
        proposal is None
        or attempt is None
        or verification is None
        or (proposal_type is not None and proposal.proposal_type != proposal_type)
        or proposal.status != "approved"
        or not proposal_binding_is_valid(proposal)
        or attempt.status != "verified"
        or verification.status != "passed"
        or verification.verification_version != "monthly-close-verification-v2"
        or attempt.proposal_binding_hash != proposal.proposal_binding_hash
    ):
        return False

    approvals = list(
        await db.scalars(
            locked(
                select(MonthlyCloseApproval)
                .where(MonthlyCloseApproval.proposal_id == proposal.proposal_id)
                .order_by(
                    MonthlyCloseApproval.sequence_no,
                    MonthlyCloseApproval.approval_id,
                )
            ).execution_options(populate_existing=True)
        )
    )
    required = int(proposal.approval_policy_snapshot.get("required_approvals", 0))
    approval_ids = [item.approval_id for item in approvals]
    if (
        required < 1
        or len(approvals) < required
        or any(
            item.decision != "approved"
            or item.payload_hash != proposal.payload_hash
            or item.proposal_binding_hash != proposal.proposal_binding_hash
            or item.policy_version
            != proposal.approval_policy_snapshot.get("policy_version")
            or item.required_approvals != required
            or item.approval_role
            != (
                "final_close_approver"
                if proposal.proposal_type == "finalize_monthly_close"
                else "finance_approver"
            )
            or item.sequence_no != index
            for index, item in enumerate(approvals, start=1)
        )
    ):
        return False
    approvers = {
        item.user_id: item
        for item in await db.scalars(
            select(User).where(
                User.user_id.in_({item.decided_by for item in approvals})
            )
        )
    }
    if any(
        (user := approvers.get(item.decided_by)) is None
        or not user.is_active
        or getattr(user.role, "value", user.role) != "admin"
        for item in approvals
    ):
        return False

    evaluation = await db.scalar(
        locked(
            select(MonthlyCloseApprovalEvaluation).where(
                MonthlyCloseApprovalEvaluation.evaluation_id
                == attempt.approval_evaluation_id,
                MonthlyCloseApprovalEvaluation.proposal_id == proposal.proposal_id,
            )
        ).execution_options(populate_existing=True)
    )
    expected_set_hash = _approval_set_hash(proposal, approvals)
    if (
        evaluation is None
        or not evaluation.satisfied
        or evaluation.policy_version
        != proposal.approval_policy_snapshot.get("policy_version")
        or evaluation.approval_ids != approval_ids
        or evaluation.approval_set_hash != expected_set_hash
        or evaluation.proposal_binding_hash != proposal.proposal_binding_hash
        or attempt.approval_set_hash != expected_set_hash
    ):
        return False

    actor = await db.scalar(
        select(User)
        .where(User.user_id == attempt.actor_id)
        .execution_options(populate_existing=True)
    )
    if (
        attempt.actor_type != "user"
        or actor is None
        or not actor.is_active
        or getattr(actor.role, "value", actor.role) != "admin"
        or SAFE_ID_RE.fullmatch(attempt.request_id or "") is None
    ):
        return False
    commands = list(proposal.canonical_payload.get("commands") or [])
    base_key = _attempt_business_key(cycle_id, commands)
    expected_attempt_key = (
        base_key
        if attempt.attempt_no == 1
        else f"{base_key}:retry:{_sha256(attempt.request_id)[:16]}"
    )
    if attempt.idempotency_key != expected_attempt_key:
        return False

    results = attempt.command_results if isinstance(attempt.command_results, list) else []
    if len(results) != len(commands) or any(
        not isinstance(result, dict)
        or result.get("business_idempotency_key")
        != command.get("business_idempotency_key")
        or result.get("command_type") != command.get("command_type")
        or result.get("status") != "succeeded_unverified"
        or not isinstance(result.get("result"), dict)
        for command, result in zip(commands, results, strict=True)
    ):
        return False

    audit_refs = attempt.audit_refs if isinstance(attempt.audit_refs, list) else []
    if (
        not audit_refs
        or not all(isinstance(item, str) for item in audit_refs)
        or audit_refs != sorted(set(audit_refs))
    ):
        return False
    try:
        audit_ids = [int(item) for item in audit_refs]
    except (TypeError, ValueError):
        return False
    audits = list(
        await db.scalars(
            locked(select(AuditLog).where(AuditLog.log_id.in_(audit_ids)))
            .execution_options(populate_existing=True)
        )
    )
    if {item.log_id for item in audits} != set(audit_ids):
        return False
    matched_keys: set[str] = set()
    for audit in audits:
        matches = [
            command
            for command in commands
            if audit.operator_id == attempt.actor_id
            and audit.before_data == command.get("before")
            and isinstance(audit.after_data, dict)
            and audit.after_data.get("approved_after") == command.get("after")
            and audit.after_data.get("business_idempotency_key")
            == command.get("business_idempotency_key")
            and audit.after_data.get("proposal_id") == proposal.proposal_id
            and audit.after_data.get("request_id") == attempt.request_id
        ]
        if len(matches) != 1:
            return False
        matched_keys.add(str(matches[0]["business_idempotency_key"]))
    if matched_keys != {
        str(command["business_idempotency_key"]) for command in commands
    }:
        return False
    if expected_audit_identity is not None:
        exact_execution_evidence = await authoritative_execution_evidence_is_valid(
            db,
            proposal=proposal,
            attempt=attempt,
            expected_audit_identity=expected_audit_identity,
            lock=lock,
        )
        if not exact_execution_evidence:
            return False

    checks = verification.checks if isinstance(verification.checks, dict) else {}
    command_checks = checks.get("commands")
    lineage_hash = execution_lineage_hash(proposal, attempt)
    expected_versions = _expected_verified_subject_versions(proposal)
    if (
        checks.get("outcome") != "applied"
        or SAFE_ID_RE.fullmatch(str(checks.get("request_id", ""))) is None
        or checks.get("execution_lineage_hash") != lineage_hash
        or not isinstance(command_checks, list)
        or len(command_checks) != len(commands)
        or any(
            not isinstance(item, dict)
            or item.get("business_idempotency_key")
            != command.get("business_idempotency_key")
            or item.get("command_type") != command.get("command_type")
            or item.get("outcome") != "applied"
            or HASH_RE.fullmatch(str(item.get("evidence_hash", ""))) is None
            or item.get("checks")
            != {
                "audit_present": True,
                "business_identity_matches": True,
                "idempotency_present": True,
                "issue_evidence_matches": True,
                "target_and_amount_match": True,
                "typed_evidence_valid": True,
            }
            for command, item in zip(commands, command_checks, strict=True)
        )
        or verification.verified_subject_versions != expected_versions
        or verification.evidence_hash
        != verified_execution_evidence_hash(
            attempt_id=attempt.attempt_id,
            command_checks=command_checks,
            outcome="applied",
            lineage_hash=lineage_hash,
            verification_request_id=str(checks.get("request_id", "")),
            verified_subject_versions=expected_versions,
        )
    ):
        return False
    return True


async def _current_approval_evaluation(
    db: AsyncSession, proposal: MonthlyCloseProposal
) -> MonthlyCloseApprovalEvaluation:
    approvals = list(
        await db.scalars(
            select(MonthlyCloseApproval)
            .where(
                MonthlyCloseApproval.proposal_id == proposal.proposal_id,
                MonthlyCloseApproval.decision == "approved",
            )
            .order_by(MonthlyCloseApproval.sequence_no, MonthlyCloseApproval.approval_id)
        )
    )
    if len(approvals) < int(proposal.approval_policy_snapshot["required_approvals"]):
        raise MonthlyCloseControlError("approval_incomplete", "方案尚未满足审批策略")
    if any(
        approval.proposal_binding_hash != proposal.proposal_binding_hash
        for approval in approvals
    ):
        raise MonthlyCloseControlError(
            "approval_binding_changed", "审批集合与当前方案不再一致"
        )
    evaluation = await db.scalar(
        select(MonthlyCloseApprovalEvaluation)
        .where(MonthlyCloseApprovalEvaluation.proposal_id == proposal.proposal_id)
        .order_by(
            MonthlyCloseApprovalEvaluation.evaluated_at.desc(),
            MonthlyCloseApprovalEvaluation.evaluation_id.desc(),
        )
        .limit(1)
    )
    expected_hash = _approval_set_hash(proposal, approvals)
    if (
        evaluation is None
        or not evaluation.satisfied
        or evaluation.policy_version
        != proposal.approval_policy_snapshot["policy_version"]
        or evaluation.approval_ids != [item.approval_id for item in approvals]
        or evaluation.approval_set_hash != expected_hash
        or evaluation.proposal_binding_hash != proposal.proposal_binding_hash
    ):
        raise MonthlyCloseControlError(
            "approval_binding_changed", "审批集合与当前方案不再一致"
        )
    return evaluation


async def _unknown_blockers(
    db: AsyncSession, cycle_id: str, proposal_id: str
) -> MonthlyCloseExecutionAttempt | None:
    return await db.scalar(
        select(MonthlyCloseExecutionAttempt)
        .where(
            MonthlyCloseExecutionAttempt.cycle_id == cycle_id,
            MonthlyCloseExecutionAttempt.proposal_id == proposal_id,
            MonthlyCloseExecutionAttempt.status.in_(("executing", "unknown")),
        )
        .order_by(MonthlyCloseExecutionAttempt.created_at.desc())
        .limit(1)
    )


async def _ensure_remediation(
    db: AsyncSession, attempt: MonthlyCloseExecutionAttempt
) -> MonthlyCloseRemediation:
    existing = await db.scalar(
        select(MonthlyCloseRemediation).where(
            MonthlyCloseRemediation.attempt_id == attempt.attempt_id
        )
    )
    if existing is not None:
        return existing
    issue = await db.scalar(
        select(MonthlyCloseIssueInstance).where(
            MonthlyCloseIssueInstance.cycle_id == attempt.cycle_id,
            MonthlyCloseIssueInstance.adapter_type == "execution",
            MonthlyCloseIssueInstance.source_subject_id == attempt.attempt_id,
            MonthlyCloseIssueInstance.issue_key == "execution_outcome_unknown",
        )
    )
    evidence_hash = _sha256(
        {"attempt_id": attempt.attempt_id, "status": attempt.status}
    )
    if issue is None:
        issue = MonthlyCloseIssueInstance(
            issue_id=_identifier("MCI-", 20),
            cycle_id=attempt.cycle_id,
            adapter_type="execution",
            source_subject_id=attempt.attempt_id,
            issue_key="execution_outcome_unknown",
            status="open",
            last_seen_evidence_hash=evidence_hash,
        )
        db.add(issue)
        await db.flush()
    remediation = MonthlyCloseRemediation(
        remediation_id=_identifier("MCRM-", 19),
        cycle_id=attempt.cycle_id,
        attempt_id=attempt.attempt_id,
        issue_id=issue.issue_id,
        status="open",
        observed_state="database commit acknowledgement was not conclusive",
        evidence_refs=[],
    )
    db.add(remediation)
    await db.flush()
    return remediation


async def _persist_ambiguous_attempt(
    db: AsyncSession,
    *,
    attempt_id: str,
    cycle_id: str,
    proposal_id: str,
    evaluation_id: str,
    approval_set_hash: str,
    proposal_binding_hash: str,
    attempt_no: int,
    request_id: str,
    idempotency_key: str,
    actor_id: str,
    command_results: list[dict[str, Any]],
    audit_refs: list[str],
) -> MonthlyCloseExecutionAttempt:
    await db.rollback()
    attempt = await db.scalar(
        select(MonthlyCloseExecutionAttempt)
        .where(MonthlyCloseExecutionAttempt.attempt_id == attempt_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    now = datetime.now(timezone.utc)
    if attempt is not None and attempt.status in TERMINAL_ATTEMPT_STATUSES:
        return attempt
    if attempt is None:
        attempt = MonthlyCloseExecutionAttempt(
            attempt_id=attempt_id,
            cycle_id=cycle_id,
            proposal_id=proposal_id,
            approval_evaluation_id=evaluation_id,
            approval_set_hash=approval_set_hash,
            proposal_binding_hash=proposal_binding_hash,
            attempt_no=attempt_no,
            request_id=request_id,
            idempotency_key=idempotency_key,
            status="unknown",
            actor_type="user",
            actor_id=actor_id,
            command_results=command_results,
            audit_refs=audit_refs,
            started_at=now,
            finished_at=now,
        )
        db.add(attempt)
        await db.flush()
    else:
        attempt.status = "unknown"
        attempt.finished_at = now
    await _ensure_remediation(db, attempt)
    await log_action_tx(
        db,
        actor_id,
        "monthly_close.execution.unknown",
        "monthly_close_execution_attempt",
        attempt.attempt_id,
        after_data={
            "attempt_id": attempt.attempt_id,
            "proposal_id": proposal_id,
            "request_id": request_id,
            "status": "unknown",
        },
    )
    await db.commit()
    await db.refresh(attempt)
    return attempt


async def _bind_command_execution_audits(
    db: AsyncSession,
    *,
    proposal: MonthlyCloseProposal,
    attempt: MonthlyCloseExecutionAttempt,
    command: dict[str, Any],
    command_result: dict[str, Any],
    identities: Any,
) -> None:
    """Persist the exact command/result identity into each handler audit row."""

    if not isinstance(identities, list) or not identities:
        raise MonthlyCloseControlError(
            "execution_audit_binding_invalid",
            "执行处理器未返回完整审计绑定",
        )
    expected_refs = command_result.get("audit_refs")
    identity_refs = [
        str(item.get("audit_ref"))
        for item in identities
        if isinstance(item, dict) and item.get("audit_ref") is not None
    ]
    if (
        not isinstance(expected_refs, list)
        or identity_refs != expected_refs
        or len(identity_refs) != len(identities)
    ):
        raise MonthlyCloseControlError(
            "execution_audit_binding_invalid",
            "执行处理器审计引用与执行结果不一致",
        )
    for raw_identity in identities:
        identity = _canonical_value(raw_identity)
        if set(identity) != {
            "action",
            "audit_ref",
            "resource_id",
            "resource_type",
            "result",
        }:
            raise MonthlyCloseControlError(
                "execution_audit_binding_invalid",
                "执行处理器审计身份不完整",
            )
        try:
            audit_id = int(identity["audit_ref"])
        except (TypeError, ValueError) as exc:
            raise MonthlyCloseControlError(
                "execution_audit_binding_invalid",
                "执行处理器审计引用无效",
            ) from exc
        audit = await db.get(AuditLog, audit_id)
        after_data = (
            audit.after_data
            if audit is not None and isinstance(audit.after_data, dict)
            else {}
        )
        if (
            audit is None
            or audit.operator_id != attempt.actor_id
            or audit.action != identity["action"]
            or audit.resource_type != identity["resource_type"]
            or audit.resource_id != identity["resource_id"]
            or audit.before_data != command.get("before")
            or after_data.get("approved_after") != command.get("after")
            or after_data.get("business_idempotency_key")
            != command.get("business_idempotency_key")
            or after_data.get("proposal_id") != proposal.proposal_id
            or after_data.get("request_id") != attempt.request_id
            or identity["result"] != command_result.get("result")
        ):
            raise MonthlyCloseControlError(
                "execution_audit_binding_invalid",
                "执行处理器结果与事务审计不一致",
            )
        audit.after_data = {
            **after_data,
            "execution_binding": {
                "action": identity["action"],
                "after": command.get("after"),
                "attempt_id": attempt.attempt_id,
                "audit_ref": identity["audit_ref"],
                "before": command.get("before"),
                "business_idempotency_key": command.get(
                    "business_idempotency_key"
                ),
                "command_id": command_result["command_id"],
                "command_type": command.get("command_type"),
                "proposal_id": proposal.proposal_id,
                "request_id": attempt.request_id,
                "resource_id": identity["resource_id"],
                "resource_type": identity["resource_type"],
                "result": identity["result"],
            },
        }


async def execute_approved_proposal(
    db: AsyncSession,
    proposal: MonthlyCloseProposal,
    actor: Any,
    *,
    request_id: str,
    handler_registry: CommandHandlerRegistry | None = None,
    commit_callback: Callable[[], Awaitable[None]] | None = None,
) -> MonthlyCloseExecutionAttempt:
    current_actor = await _current_user(
        db,
        actor,
        allowed_roles=frozenset({"admin"}),
        forbidden_code="execution_forbidden",
    )
    request_id = _validate_request_id(request_id)
    registry = handler_registry or _default_handler_registry()
    if proposal.proposal_type in {
        "ota_reconciliation",
        "ota_appeal_adjudication",
        "service_fee_reconciliation",
        "utility_reconciliation",
        "operating_expense_import",
        "generate_owner_settlements",
    }:
        # Acquire the cross-ledger boundary before the existing cycle lock and
        # before an attempt/idempotency row is staged.  A settlement that wins
        # therefore makes proposal freshness fail without leaving a failed
        # execution record behind.
        from app.services.monthly_close.financial_lock import (
            acquire_month_financial_lock,
        )

        billing_month = await db.scalar(
            select(MonthlyCloseCycle.billing_month).where(
                MonthlyCloseCycle.cycle_id == proposal.cycle_id
            )
        )
        if billing_month is None:
            raise MonthlyCloseControlError("cycle_not_found", "月结周期不存在")
        months = {billing_month}
        if proposal.proposal_type == "ota_appeal_adjudication":
            for command in proposal.canonical_payload.get("commands", []):
                later_month = (command.get("before") or {}).get(
                    "later_billing_month"
                )
                if isinstance(later_month, str):
                    months.add(later_month)
        for month in sorted(months):
            await acquire_month_financial_lock(db, month)
    cycle, locked = await _locked_proposal(db, proposal)
    cycle_id = cycle.cycle_id
    proposal_id = locked.proposal_id
    proposal_binding_hash = locked.proposal_binding_hash
    actor_id = current_actor.user_id
    same_request = await db.scalar(
        select(MonthlyCloseExecutionAttempt).where(
            MonthlyCloseExecutionAttempt.proposal_id == proposal_id,
            MonthlyCloseExecutionAttempt.request_id == request_id,
        )
    )
    if same_request is not None:
        return same_request
    if locked.status != "approved":
        raise MonthlyCloseControlError("proposal_not_approved", "方案尚未批准")
    commands = list(locked.canonical_payload["commands"])
    business_key = _attempt_business_key(cycle.cycle_id, commands)

    current_commands_by_key = {
        command["business_idempotency_key"]: command for command in commands
    }
    current_business_keys = set(current_commands_by_key)
    prior_rows = list(
        (
            await db.execute(
                select(MonthlyCloseExecutionAttempt, MonthlyCloseProposal)
                .join(
                    MonthlyCloseProposal,
                    MonthlyCloseProposal.proposal_id
                    == MonthlyCloseExecutionAttempt.proposal_id,
                )
                .where(
                    MonthlyCloseExecutionAttempt.cycle_id == cycle_id,
                    MonthlyCloseExecutionAttempt.status.not_in(
                        ("failed_safe", "failed_confirmed")
                    ),
                )
                .order_by(MonthlyCloseExecutionAttempt.created_at)
            )
        ).all()
    )
    business_conflict = False
    for prior_attempt, prior_proposal in prior_rows:
        prior_commands_by_key = {
            item["business_idempotency_key"]: item
            for item in prior_proposal.canonical_payload.get("commands", [])
        }
        prior_business_keys = set(prior_commands_by_key)
        if not current_business_keys.intersection(prior_business_keys):
            continue
        if (
            current_business_keys == prior_business_keys
            and current_commands_by_key == prior_commands_by_key
        ):
            return prior_attempt
        business_conflict = True
    prior_business = await db.scalar(
        select(MonthlyCloseExecutionAttempt)
        .where(
            MonthlyCloseExecutionAttempt.cycle_id == cycle.cycle_id,
            MonthlyCloseExecutionAttempt.idempotency_key == business_key,
        )
        .order_by(MonthlyCloseExecutionAttempt.created_at)
        .limit(1)
    )
    if (
        not business_conflict
        and prior_business is not None
        and prior_business.status not in {
        "failed_safe",
        "failed_confirmed",
        }
    ):
        return prior_business
    blocker = await _unknown_blockers(db, cycle.cycle_id, locked.proposal_id)
    if blocker is not None:
        return blocker
    if cycle.status == "completed":
        raise MonthlyCloseControlError("cycle_completed", "已完成月结不能执行新写入")
    if business_conflict:
        raise MonthlyCloseControlError(
            "business_idempotency_conflict",
            "方案包含已经执行或结果待确认的业务命令，请重新生成方案",
        )
    if not await _proposal_is_current(db, cycle, locked, registry):
        await _mark_stale_and_raise(db, locked, current_actor.user_id)

    evaluation = await _current_approval_evaluation(db, locked)
    maximum_attempt = int(
        await db.scalar(
            select(func.coalesce(func.max(MonthlyCloseExecutionAttempt.attempt_no), 0)).where(
                MonthlyCloseExecutionAttempt.proposal_id == locked.proposal_id
            )
        )
        or 0
    )
    attempt_no = maximum_attempt + 1
    attempt_key = (
        business_key
        if prior_business is None
        else f"{business_key}:retry:{_sha256(request_id)[:16]}"
    )
    now = datetime.now(timezone.utc)
    attempt_id = _identifier("MCAT-", 19)
    attempt = MonthlyCloseExecutionAttempt(
        attempt_id=attempt_id,
        cycle_id=cycle.cycle_id,
        proposal_id=locked.proposal_id,
        approval_evaluation_id=evaluation.evaluation_id,
        approval_set_hash=evaluation.approval_set_hash,
        proposal_binding_hash=proposal_binding_hash,
        attempt_no=attempt_no,
        request_id=request_id,
        idempotency_key=attempt_key,
        status="executing",
        actor_type="user",
        actor_id=current_actor.user_id,
        command_results=[],
        audit_refs=[],
        started_at=now,
    )
    db.add(attempt)
    try:
        await db.flush()
    except IntegrityError:
        # A concurrent request for the exact same approved business effect may
        # win the database uniqueness race after our freshness read.  Converge
        # on that durable attempt; never weaken the unique constraint or create
        # a second mutation.
        await db.rollback()
        winner = await db.scalar(
            select(MonthlyCloseExecutionAttempt).where(
                MonthlyCloseExecutionAttempt.cycle_id == cycle_id,
                MonthlyCloseExecutionAttempt.idempotency_key == attempt_key,
                MonthlyCloseExecutionAttempt.proposal_id == proposal_id,
            )
        )
        if winner is not None:
            return winner
        raise

    unregistered = [
        command["command_type"]
        for command in commands
        if registry.get(command["command_type"]) is None
    ]
    if unregistered:
        attempt.status = "failed_safe"
        attempt.finished_at = datetime.now(timezone.utc)
        attempt.command_results = [
            {
                "code": "COMMAND_HANDLER_UNREGISTERED",
                "command_type": command_type,
                "status": "failed_safe",
            }
            for command_type in unregistered
        ]
        await log_action_tx(
            db,
            current_actor.user_id,
            "monthly_close.execution.failed_safe",
            "monthly_close_execution_attempt",
            attempt.attempt_id,
            after_data={
                "attempt_id": attempt.attempt_id,
                "code": "COMMAND_HANDLER_UNREGISTERED",
                "proposal_id": locked.proposal_id,
                "request_id": request_id,
            },
        )
        await db.commit()
        await db.refresh(attempt)
        return attempt

    command_results: list[dict[str, Any]] = []
    audit_refs: list[str] = []
    try:
        for command in commands:
            handler = registry.get(command["command_type"])
            assert handler is not None
            raw_result = await handler.execute(
                db, cycle, locked, command, current_actor, request_id
            )
            safe_result = _canonical_value(raw_result.get("result") or {})
            refs = [str(item) for item in raw_result.get("audit_refs") or []]
            command_result = {
                "business_idempotency_key": command[
                    "business_idempotency_key"
                ],
                "command_type": command["command_type"],
                "result": safe_result,
                "status": "succeeded_unverified",
            }
            execution_audits = raw_result.get("execution_audits")
            if execution_audits is not None:
                command_result = {
                    **command_result,
                    "audit_refs": sorted(set(refs)),
                    "command_id": _sha256(command),
                }
                await _bind_command_execution_audits(
                    db,
                    proposal=locked,
                    attempt=attempt,
                    command=command,
                    command_result=command_result,
                    identities=execution_audits,
                )
            command_results.append(command_result)
            audit_refs.extend(refs)
        attempt.command_results = command_results
        attempt.audit_refs = sorted(set(audit_refs))
        attempt.status = "succeeded_unverified"
        attempt.finished_at = datetime.now(timezone.utc)
        await log_action_tx(
            db,
            current_actor.user_id,
            "monthly_close.execution.succeeded_unverified",
            "monthly_close_execution_attempt",
            attempt.attempt_id,
            after_data={
                "attempt_id": attempt.attempt_id,
                "business_idempotency_keys": [
                    item["business_idempotency_key"] for item in commands
                ],
                "proposal_id": locked.proposal_id,
                "request_id": request_id,
            },
        )
        try:
            if commit_callback is None:
                await db.commit()
            else:
                await commit_callback()
        except IntegrityError:
            # A constraint rejection proves the transaction did not commit.
            raise
        except (CommitOutcomeUnknown, DBAPIError):
            return await _persist_ambiguous_attempt(
                db,
                attempt_id=attempt_id,
                cycle_id=cycle_id,
                proposal_id=proposal_id,
                evaluation_id=evaluation.evaluation_id,
                approval_set_hash=evaluation.approval_set_hash,
                proposal_binding_hash=proposal_binding_hash,
                attempt_no=attempt_no,
                request_id=request_id,
                idempotency_key=attempt_key,
                actor_id=actor_id,
                command_results=command_results,
                audit_refs=sorted(set(audit_refs)),
            )
    except CommitOutcomeUnknown:
        return await _persist_ambiguous_attempt(
            db,
            attempt_id=attempt_id,
            cycle_id=cycle_id,
            proposal_id=proposal_id,
            evaluation_id=evaluation.evaluation_id,
            approval_set_hash=evaluation.approval_set_hash,
            proposal_binding_hash=proposal_binding_hash,
            attempt_no=attempt_no,
            request_id=request_id,
            idempotency_key=attempt_key,
            actor_id=actor_id,
            command_results=command_results,
            audit_refs=sorted(set(audit_refs)),
        )
    except Exception as exc:
        await db.rollback()
        # Every registered Task 6 handler is DB-only.  A rollback before commit
        # proves no business transaction committed; external adapters must raise
        # CommitOutcomeUnknown instead of entering this branch.
        cycle, locked = await _locked_proposal_by_ids(db, cycle_id, proposal_id)
        evaluation = await _current_approval_evaluation(db, locked)
        safe_failure_code = (
            exc.code if isinstance(exc, MonthlyCloseControlError) else "COMMAND_REJECTED"
        )
        attempt = MonthlyCloseExecutionAttempt(
            attempt_id=attempt_id,
            cycle_id=cycle.cycle_id,
            proposal_id=locked.proposal_id,
            approval_evaluation_id=evaluation.evaluation_id,
            approval_set_hash=evaluation.approval_set_hash,
            proposal_binding_hash=locked.proposal_binding_hash,
            attempt_no=attempt_no,
            request_id=request_id,
            idempotency_key=attempt_key,
            status="failed_safe",
            actor_type="user",
            actor_id=actor_id,
            command_results=[{"code": safe_failure_code, "status": "failed_safe"}],
            audit_refs=[],
            started_at=now,
            finished_at=datetime.now(timezone.utc),
        )
        db.add(attempt)
        await log_action_tx(
            db,
            actor_id,
            "monthly_close.execution.failed_safe",
            "monthly_close_execution_attempt",
            attempt.attempt_id,
            after_data={
                "attempt_id": attempt.attempt_id,
                "code": safe_failure_code,
                "proposal_id": locked.proposal_id,
                "request_id": request_id,
            },
        )
        try:
            await db.commit()
        except IntegrityError:
            # The successful concurrent execution can commit while this
            # request is rolling back a rejected duplicate handler call.
            await db.rollback()
            winner = await db.scalar(
                select(MonthlyCloseExecutionAttempt).where(
                    MonthlyCloseExecutionAttempt.cycle_id == cycle_id,
                    MonthlyCloseExecutionAttempt.idempotency_key == attempt_key,
                    MonthlyCloseExecutionAttempt.proposal_id == proposal_id,
                )
            )
            if winner is not None:
                return winner
            raise
        await db.refresh(attempt)
        return attempt

    await db.refresh(attempt)
    return attempt
