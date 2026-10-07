"""Monthly-close queries, evidence-backed investigations and confirmed cleaning plans.

The model may choose exactly one server-defined read query or a clarification.
Every fact and next action comes from the role projection; model output never
names a database field, URL, arbitrary object ID, or write command. Cleaning
investigations use server-owned business selectors, live evidence tools and
actor-scoped durable follow-up state. Cleaning mutations use separate server-built
plans bound to live evidence and explicit administrator confirmation.
"""

from __future__ import annotations

import asyncio
import json
import re
import unicodedata
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any, Literal
from uuid import uuid4

from openai import AsyncOpenAI
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as postgresql_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.monthly_close import (
    MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES,
    MonthlyCloseCycle,
    MonthlyCloseInboxItem,
)
from app.models.monthly_close_control import (
    MonthlyCloseConversation,
    MonthlyCloseMessage,
    MonthlyCloseRun,
)
from app.models.user import User
from app.services.monthly_close import semantic_agent
from app.services.monthly_close.cleaning_investigation import (
    CLEANING_TOOL_VERSION,
    CleaningInvestigationFacts,
    InvestigationFinding,
    investigate_cleaning,
    parse_cleaning_request,
)
from app.services.monthly_close.cleaning_investigation import (
    CleaningRequest as CleaningInvestigationRequest,
)
from app.services.monthly_close.cleaning_work_chat import (
    VERSION as WORK_CHAT_VERSION,
)
from app.services.monthly_close.cleaning_work_chat import (
    WorkChatFacts,
    answer_work_chat,
)
from app.services.monthly_close.conversation import (
    GUIDANCE,
    LABELS,
    ProgressFacts,
    progress_request,
    read_progress,
)
from app.services.monthly_close.conversation import (
    VERSION as PROGRESS_CHAT_VERSION,
)
from app.services.monthly_close.events import append_cycle_event
from app.services.monthly_close.inbox import apply_inbox_source_hint
from app.services.monthly_close.investigation_agent import (
    AGENT_VERSION,
    business_question,
    correction_draft,
    reason_over_evidence,
)
from app.services.monthly_close.projection import (
    MonthlyCloseProjection,
    build_monthly_close_projection,
    get_visible_projection_document,
)
from app.services.monthly_close.semantic_agent import AgentClarificationFacts
from app.services.monthly_close import order_identity_chat
from app.services.monthly_close.order_identity_chat import OrderIdentityFacts
from app.services.monthly_close import conversation_context
from app.services.monthly_close.conversation_context import ConversationMemoryFacts
from app.services.financial_case.chat import FinancialCaseFacts
from app.services.financial_case import chat as financial_case_chat

ASSISTANT_PROMPT_VERSION = "monthly-close-read-only/v1"
TOOL_MANIFEST_VERSION = "monthly-close-read-tools/v1"
ASSISTANT_RUN_MODEL_TIMEOUT_SECONDS = 60
ASSISTANT_RUN_RECOVERY_GRACE_SECONDS = 30
READ_TOOLS = (
    "get_month_status",
    "get_source_status",
    "get_document_status",
    "get_issue_detail",
    "get_recommended_action",
    "explain_reconciliation_result",
)
_SOURCE_LABELS = {
    "cleaning_statement": "保洁打扫记录",
    "linen_statement": "布草／洗涤记录",
    "utility_expense": "水电支出",
    "ota_statement": "OTA 平台账单",
    "operating_expenses": "其他运营支出",
}
from app.services.monthly_close.result_output import FormattedResultFacts, ResultOutput, build_document, output_request, present_document

ReadTool = Literal[
    "formatted_result",
    "get_month_status",
    "get_source_status",
    "get_document_status",
    "get_issue_detail",
    "get_recommended_action",
    "explain_reconciliation_result",
    "investigate_cleaning",
    "cleaning_work_chat",
    "review_month",
    "agent_clarification",
    "order_identity_chat",
    "conversation_memory",
    "financial_case",
]
SourceType = Literal[
    "cleaning_statement",
    "linen_statement",
    "utility_expense",
    "ota_statement",
    "operating_expenses",
]
ClarificationCode = Literal["choose_read_topic", "select_attachment"]
CLARIFICATION_TEXT: dict[str, str] = {
    "choose_read_topic": "你想查看本月状态、某类资料，还是当前下一步？",
    "select_attachment": "请先选择当前月份中你有权限查看的附件。",
}


class AssistantAttachmentNotFound(LookupError):
    """An attachment is missing, outside the cycle, or invisible to this actor."""


class AssistantAuthorizationChanged(PermissionError):
    """The actor changed or became inactive while the model was running."""


class _ModelOutputInvalid(ValueError):
    """Schema-valid model output that violates a server-side tool precondition."""


class ToolArguments(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_type: SourceType | None = None


class AssistantDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")
    intent: Literal["read_query", "clarification", "write_request", "sensitive_input"]
    tool: ReadTool | None = None
    arguments: ToolArguments = Field(default_factory=ToolArguments)
    clarification_code: ClarificationCode | None = None

    @model_validator(mode="after")
    def validate_shape(self):
        if self.intent == "read_query" and self.tool is None:
            raise ValueError("read query requires one allowlisted tool")
        if self.intent != "read_query" and self.tool is not None and not (self.intent == "clarification" and self.tool == "agent_clarification"):
            raise ValueError("non-query decision cannot call a tool")
        if self.intent == "clarification" and self.clarification_code is None:
            raise ValueError("clarification requires a server-owned code")
        if self.intent != "clarification" and self.clarification_code is not None:
            raise ValueError("clarification code is invalid for this intent")
        if self.tool == "get_source_status" and self.arguments.source_type is None:
            raise ValueError("source query requires an allowlisted source type")
        if self.tool != "get_source_status" and self.arguments.source_type is not None:
            raise ValueError("source_type is not valid for this tool")
        return self


class _SafeReplyDTO(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _safe_reply_code(value: str) -> str:
    if not isinstance(value, str) or not re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,159}", value
    ):
        raise ValueError("unsafe assistant structured value")
    return value


class RecommendedActionDTO(_SafeReplyDTO):
    kind: str
    label: str
    reason_code: str
    target_type: str
    target_id: str

    @field_validator("kind", "reason_code", "target_type", "target_id")
    @classmethod
    def safe_identifiers(cls, value: str) -> str:
        return _safe_reply_code(value)

    @field_validator("label")
    @classmethod
    def safe_label(cls, value: str) -> str:
        if len(value) > 160 or re.search(r"https?://|(?<!\d)1[3-9]\d{9}(?!\d)", value):
            raise ValueError("unsafe recommended action label")
        return value


class MonthStatusFacts(_SafeReplyDTO):
    projection_version: Literal["monthly-close-projection/v1"]
    cycle_id: str
    billing_month: str
    cycle_status: str
    source_states: dict[SourceType, str]

    @field_validator("cycle_id", "billing_month", "cycle_status")
    @classmethod
    def safe_codes(cls, value: str) -> str:
        return _safe_reply_code(value)

    @field_validator("source_states")
    @classmethod
    def safe_source_states(cls, value: dict[str, str]) -> dict[str, str]:
        return {key: _safe_reply_code(state) for key, state in value.items()}


class SourceStatusFacts(_SafeReplyDTO):
    source_type: SourceType
    state: str

    @field_validator("state")
    @classmethod
    def safe_state(cls, value: str) -> str:
        return _safe_reply_code(value)


class SafeDocumentFacts(_SafeReplyDTO):
    document_id: str | None = None
    item_id: str | None = None
    source_type: SourceType | None = None
    storage_state: str
    classification_state: str
    analysis_state: str
    inbox_state: str | None = None

    @field_validator(
        "document_id",
        "item_id",
        "storage_state",
        "classification_state",
        "analysis_state",
        "inbox_state",
    )
    @classmethod
    def safe_codes(cls, value: str | None) -> str | None:
        return _safe_reply_code(value) if value is not None else None

    @model_validator(mode="after")
    def require_one_attachment_identity(self):
        if (self.document_id is None) == (self.item_id is None):
            raise ValueError("attachment facts require exactly one identity")
        return self


class DocumentStatusFacts(_SafeReplyDTO):
    documents: list[SafeDocumentFacts]
    request_text: str | None = None
    billing_month: str | None = None
    work_record_count: int | None = Field(default=None, ge=0)


class IssueDetailFacts(_SafeReplyDTO):
    issue: RecommendedActionDTO | None
    reason: Literal["no_visible_recommended_issue"] | None = None


class RecommendedActionFacts(_SafeReplyDTO):
    projection_version: Literal["monthly-close-projection/v1"]
    recommended_action: RecommendedActionDTO


class ReconciliationFacts(_SafeReplyDTO):
    projection_version: Literal["monthly-close-projection/v1"]
    blocker_codes: list[str]

    @field_validator("blocker_codes")
    @classmethod
    def safe_blocker_codes(cls, value: list[str]) -> list[str]:
        return [_safe_reply_code(item) for item in value]


class WriteRequestFacts(_SafeReplyDTO):
    projection_version: Literal["monthly-close-projection/v1"]
    write_performed: Literal[False]
    safe_next_step: Literal["create_proposal"]


class SensitiveInputFacts(_SafeReplyDTO):
    projection_version: Literal["monthly-close-projection/v1"]
    sensitive_input: Literal[True]


class ClarificationFacts(_SafeReplyDTO):
    projection_version: Literal["monthly-close-projection/v1"] | None = None
    authorization_changed: Literal[True] | None = None


class PromptSourceDTO(_SafeReplyDTO):
    source_type: SourceType
    state: str

    @field_validator("state")
    @classmethod
    def safe_state(cls, value: str) -> str:
        return _safe_reply_code(value)


class PromptProjectionDTO(_SafeReplyDTO):
    projection_version: Literal["monthly-close-projection/v1"]
    cycle_status: str
    actor_role: Literal["admin", "finance", "operator", "cleaner", "keeper"]
    sources: list[PromptSourceDTO]
    recommended_action: RecommendedActionDTO

    @field_validator("cycle_status")
    @classmethod
    def safe_cycle_status(cls, value: str) -> str:
        return _safe_reply_code(value)


class AssistantReply(_SafeReplyDTO):
    message: str
    intent: Literal["read_query", "clarification", "write_request", "sensitive_input", "action_plan", "action_result"]
    tool: ReadTool | None
    facts: dict[str, Any]
    recommended_action: dict[str, Any]
    narration_degraded: bool
    conversation_id: str
    run_id: str
    output: ResultOutput | None = None

    @model_validator(mode="after")
    def validate_server_owned_structure(self):
        if self.intent == "read_query" and self.tool is None:
            raise ValueError("read reply requires one allowlisted tool")
        if self.intent not in {"read_query", "action_plan", "action_result"} and self.tool is not None and not (self.intent == "clarification" and self.tool == "agent_clarification"):
            raise ValueError("non-read reply cannot expose a tool")
        facts_model: type[_SafeReplyDTO]
        if self.tool == "formatted_result":
            facts_model = FormattedResultFacts
        elif self.tool == "financial_case":
            facts_model = FinancialCaseFacts
        elif self.tool == "cleaning_work_chat":
            facts_model = WorkChatFacts
        elif self.tool == "conversation_memory":
            facts_model = ConversationMemoryFacts
        elif self.tool == "order_identity_chat":
            facts_model = OrderIdentityFacts
        elif self.tool == "agent_clarification" and self.intent == "clarification":
            facts_model = AgentClarificationFacts
        elif self.tool == "review_month" and self.intent == "read_query":
            facts_model = ProgressFacts
        elif self.intent in {"action_plan", "action_result"}:
            raise ValueError("controlled actions require a server-owned operation tool")
        elif self.intent == "write_request":
            facts_model = WriteRequestFacts
        elif self.intent == "sensitive_input":
            facts_model = SensitiveInputFacts
        elif self.intent == "clarification":
            facts_model = ClarificationFacts
        else:
            selected_facts_model = {
                "get_month_status": MonthStatusFacts,
                "get_source_status": SourceStatusFacts,
                "get_document_status": DocumentStatusFacts,
                "get_issue_detail": IssueDetailFacts,
                "get_recommended_action": RecommendedActionFacts,
                "explain_reconciliation_result": ReconciliationFacts,
                "investigate_cleaning": CleaningInvestigationFacts,
            }.get(self.tool)
            if selected_facts_model is None:
                raise ValueError("read reply tool is invalid")
            facts_model = selected_facts_model
        legacy_work_spec = (
            facts_model is WorkChatFacts and isinstance(self.facts.get("spec"), dict)
            and "repair_fees" not in self.facts["spec"]
        )
        self.facts = facts_model.model_validate(self.facts).model_dump(
            mode="json", exclude_none=facts_model not in {CleaningInvestigationFacts, FinancialCaseFacts}
        )
        if legacy_work_spec:
            # Preserve the canonical bytes used to verify pre-fee chat history.
            self.facts["spec"].pop("repair_fees", None)
        if facts_model is FinancialCaseFacts and self.facts.get("report_snapshot") is None:
            # Preserve hashes of financial replies saved before full report exports.
            self.facts.pop("report_snapshot", None)
        if self.recommended_action:
            self.recommended_action = RecommendedActionDTO.model_validate(
                self.recommended_action
            ).model_dump(mode="json")
        if len(self.message) > 500 or re.search(
            r"https?://|(?<!\d)1[3-9](?:[\s-]?\d){9}(?!\d)|"
            r"(?i:(?:token|secret|password|api[_-]?key)\s*[:=])",
            self.message,
        ):
            raise ValueError("assistant reply contains unsafe prose")
        return self

    def to_dict(self) -> dict[str, Any]:
        payload = self.model_dump(mode="json")
        if self.output is None:
            payload.pop("output", None)  # Preserve hashes of all prior durable replies.
        return payload


def _attach_requested_output(reply, text):
    request = output_request(text)
    if request and reply.intent == "read_query" and reply.facts.get("billing_month"):
        try:
            document = build_document(reply, datetime.now(timezone.utc).isoformat())
            reply.output = ResultOutput(document=present_document(document, request, text), presentation=request)
        except ValueError:
            reply.message = reply.message[:400] + "\n查询已保留，但结果超出当前整理范围。请缩小查询范围后再指定输出格式。"
    return reply


def redact_text(text: str) -> str:
    """Redact common PII, lock credentials, addresses, and inline secrets."""
    safe = text[:4000]
    safe = re.sub(r"(?<!\d)1[3-9]\d{9}(?!\d)", "[已脱敏]", safe)
    safe = re.sub(r"(?<!\d)\d{17}[\dXx](?!\w)", "[已脱敏]", safe)
    safe = re.sub(
        r"(?:门锁|房门)?(?:密码|门锁码|密码锁)\s*[:：]?\s*\d{4,10}",
        "门锁密码[已脱敏]",
        safe,
    )
    safe = re.sub(
        r"(?i)(?:token|secret|password|cookie|api[_-]?key)\s*[:=]\s*[^\s,;，；]+",
        "凭证=[已脱敏]",
        safe,
    )
    safe = re.sub(
        r"(?:完整)?地址\s*[:：]?\s*[^\s,，;；]+",
        "地址[已脱敏]",
        safe,
    )
    return safe


def _looks_sensitive(text: str) -> bool:
    normalized = unicodedata.normalize("NFKC", text)
    compact = re.sub(r"[^a-z0-9\u4e00-\u9fff]", "", normalized.lower())
    if re.search(r"(?<!\d)1[3-9](?:[\s-]?\d){9}(?!\d)", normalized):
        return True
    if re.search(r"(?<!\d)\d(?:[\s-]?\d){16}[\dXx](?!\w)", normalized):
        return True
    if any(
        marker in compact
        for marker in (
            "guestphone",
            "guestcontact",
            "apikey",
            "accesstoken",
            "intaketoken",
            "password",
            "privatekey",
            "lockcode",
            "doorcode",
            "homeaddress",
            "fulladdress",
            "workbookcells",
            "workbookcontent",
            "otasettlement",
            "settlementtotal",
            "platformcommission",
            "ownerrevenue",
        )
    ):
        return True
    if re.search(r"(?i)(?:sheet\d*!)[A-Z]{1,3}\d+", normalized):
        return True
    if re.search(r"(?:地址|住址)\s*[:=：]?\s*[^\s,，;；]{6,}", normalized):
        return True
    if re.search(
        r"(?:省|市).{1,30}(?:区|县).{1,30}(?:路|街|大道|小区).{0,20}\d+号",
        normalized,
    ):
        return True
    if re.search(
        r"(?:OTA|结算|业主收入|佣金).{0,20}\d+(?:\.\d{1,2})?", normalized, re.I
    ):
        return True
    return False


def _actor_identity(actor: Any) -> tuple[str, str]:
    if isinstance(actor, dict):
        actor_id = str(actor.get("user_id") or "")
        raw_role = actor.get("role")
    else:
        actor_id = str(getattr(actor, "user_id", ""))
        raw_role = getattr(actor, "role", None)
    role = str(getattr(raw_role, "value", raw_role) or "")
    if not actor_id or role not in {
        "admin",
        "finance",
        "operator",
        "cleaner",
        "keeper",
    }:
        raise ValueError("monthly-close assistant requires a current employee actor")
    return actor_id, role


def _audience_scope(role: str) -> str:
    if role in {"admin", "finance"}:
        return "finance"
    if role == "operator":
        return "operations"
    return "assigned_staff"


def _stable_id(prefix: str, value: str) -> str:
    return (
        prefix + sha256(value.encode("utf-8")).hexdigest()[: 24 - len(prefix)].upper()
    )


def _assistant_reply_hash(payload: dict[str, Any]) -> str:
    return sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True).encode("utf-8")
    ).hexdigest()


def _safe_legacy_terminal_reply(run: MonthlyCloseRun) -> AssistantReply:
    """Fail closed for pre-migration rows or invalid durable structured output."""
    return AssistantReply(
        message="本次查询已结束，请重新发起只读查询以获取当前状态。",
        intent="clarification",
        tool=None,
        facts={},
        recommended_action={},
        narration_degraded=True,
        conversation_id=run.conversation_id,
        run_id=run.run_id,
    )


def _validated_durable_reply(
    run: MonthlyCloseRun,
    assistant_message: MonthlyCloseMessage,
    *,
    expected_scope: str,
) -> AssistantReply:
    expected_message_id = _stable_id("MCM-", f"terminal:{run.run_id}")
    if (
        assistant_message.message_id != expected_message_id
        or assistant_message.cycle_id != run.cycle_id
        or assistant_message.run_id != run.run_id
        or assistant_message.conversation_id != run.conversation_id
        or assistant_message.role != "assistant"
        or assistant_message.visibility_scope != expected_scope
    ):
        return _safe_legacy_terminal_reply(run)
    payload = run.output_payload_redacted
    if not isinstance(payload, dict):
        return _safe_legacy_terminal_reply(run)
    try:
        reply = AssistantReply.model_validate(payload)
    except ValidationError:
        return _safe_legacy_terminal_reply(run)
    canonical_payload = reply.to_dict()
    if (
        reply.run_id != run.run_id
        or reply.conversation_id != run.conversation_id
        or assistant_message.content_redacted != reply.message
        or run.output_hash != _assistant_reply_hash(canonical_payload)
    ):
        return _safe_legacy_terminal_reply(run)
    return reply


async def _terminalize_degraded_run(
    db: AsyncSession,
    run_id: str,
    *,
    error_code: str,
    message: str,
) -> tuple[AssistantReply, bool]:
    reply = AssistantReply(
        message=message,
        intent="clarification",
        tool=None,
        facts={},
        recommended_action={},
        narration_degraded=True,
        conversation_id="pending",
        run_id=run_id,
    )
    return await _complete_run(
        db,
        run_id,
        reply=reply,
        status="degraded",
        error_code=error_code,
        error_detail_redacted="助理运行已安全终止",
    )


async def _complete_run(
    db: AsyncSession,
    run_id: str,
    *,
    reply: AssistantReply,
    status: Literal["succeeded", "waiting_user", "degraded", "failed"],
    error_code: str | None,
    error_detail_redacted: str | None,
) -> tuple[AssistantReply, bool]:
    """Atomically elect one terminal completion and return the durable winner."""
    run = await db.scalar(
        select(MonthlyCloseRun)
        .where(MonthlyCloseRun.run_id == run_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if run is None:
        raise RuntimeError("monthly-close assistant run disappeared")
    if run.status != "running":
        conversation = await db.scalar(
            select(MonthlyCloseConversation).where(
                MonthlyCloseConversation.conversation_id == run.conversation_id,
                MonthlyCloseConversation.cycle_id == run.cycle_id,
            )
        )
        if conversation is None:
            canonical = _safe_legacy_terminal_reply(run)
            await db.rollback()
            return canonical, False
        expected_scope = conversation.audience_scope
        expected_message_id = _stable_id("MCM-", f"terminal:{run.run_id}")
        assistant_message = await db.scalar(
            select(MonthlyCloseMessage).where(
                MonthlyCloseMessage.message_id == expected_message_id,
                MonthlyCloseMessage.cycle_id == run.cycle_id,
                MonthlyCloseMessage.run_id == run.run_id,
                MonthlyCloseMessage.conversation_id == run.conversation_id,
                MonthlyCloseMessage.role == "assistant",
                MonthlyCloseMessage.visibility_scope == expected_scope,
            )
        )
        assistant_message_count = await db.scalar(
            select(func.count())
            .select_from(MonthlyCloseMessage)
            .where(
                MonthlyCloseMessage.run_id == run.run_id,
                MonthlyCloseMessage.role == "assistant",
            )
        )
        if assistant_message is None or assistant_message_count != 1:
            canonical = _safe_legacy_terminal_reply(run)
        else:
            canonical = _validated_durable_reply(
                run,
                assistant_message,
                expected_scope=expected_scope,
            )
        await db.rollback()
        return canonical, False
    actor_id = run.actor_id or "SYSTEM"
    raw_role = run.permission_snapshot.get("role")
    role = (
        raw_role
        if raw_role in {"admin", "finance", "operator", "cleaner", "keeper"}
        else "operator"
    )
    conversation = await db.get(MonthlyCloseConversation, run.conversation_id)
    scope = (
        conversation.audience_scope
        if conversation is not None
        else _audience_scope(role)
    )
    reply = AssistantReply.model_validate(
        reply.model_copy(
            update={
                "conversation_id": run.conversation_id,
                "run_id": run.run_id,
            }
        ).to_dict()
    )
    safe_payload = reply.to_dict()
    assistant_message_id = _stable_id("MCM-", f"terminal:{run.run_id}")
    assistant_message = MonthlyCloseMessage(
        message_id=assistant_message_id,
        cycle_id=run.cycle_id,
        conversation_id=run.conversation_id,
        run_id=run.run_id,
        role="assistant",
        content_redacted=reply.message,
        attachments=[],
        visibility_scope=scope,
        created_by=actor_id,
    )
    db.add(assistant_message)
    await db.flush()
    run.status = status
    run.error_code = error_code
    run.error_detail_redacted = error_detail_redacted
    run.output_payload_redacted = safe_payload
    run.output_hash = _assistant_reply_hash(safe_payload)
    run.finished_at = datetime.now(timezone.utc)
    await append_cycle_event(
        db,
        run.cycle_id,
        "assistant.replied",
        {"user_id": actor_id, "role": role},
        {
            "message_id": assistant_message.message_id,
            "run_id": run.run_id,
            "intent": reply.intent,
            "narration_degraded": reply.narration_degraded,
        },
        f"assistant-replied:{assistant_message.message_id}",
    )
    await db.commit()
    return reply, True


async def reconcile_stale_assistant_runs(
    db: AsyncSession,
    *,
    now: datetime | None = None,
    limit: int = 100,
) -> int:
    """Recover process-death orphans after model timeout plus a bounded grace."""
    if limit < 1 or limit > 500:
        raise ValueError("stale assistant run batch size is invalid")
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(
        seconds=ASSISTANT_RUN_MODEL_TIMEOUT_SECONDS
        + ASSISTANT_RUN_RECOVERY_GRACE_SECONDS
    )
    stale_runs = list(
        await db.scalars(
            select(MonthlyCloseRun)
            .where(
                MonthlyCloseRun.status == "running",
                MonthlyCloseRun.trigger_type == "user_message",
                MonthlyCloseRun.conversation_id.is_not(None),
                MonthlyCloseRun.actor_id.is_not(None),
                MonthlyCloseRun.started_at.is_not(None),
                MonthlyCloseRun.started_at < cutoff,
            )
            .order_by(MonthlyCloseRun.started_at, MonthlyCloseRun.run_id)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
    )
    recovered = 0
    for run in stale_runs:
        _reply, won = await _terminalize_degraded_run(
            db,
            run.run_id,
            error_code="RUN_LEASE_EXPIRED",
            message="本次查询运行超时，已安全终止；请重新发起只读查询。",
        )
        if won:
            recovered += 1
    if not stale_runs:
        await db.rollback()
    return recovered


def _write_requested(text: str) -> bool:
    return bool(
        re.search(
            r"(?:把|请|帮我|立刻|直接|全部|所有).{0,30}(?:修改|改成|删除|批准|审批|执行|导入|完成月结|关账|结账)|^(?:批准|审批|执行|完成月结|关账)",
            text,
        )
    )


_SAFE_INTENT_ORDER = (
    "month",
    "cleaning_statement",
    "linen_statement",
    "utility_expense",
    "ota_statement",
    "operating_expenses",
    "reconciliation",
    "next_step",
    "query",
    "status",
    "write_request",
)
_SAFE_INTENT_LEXEMES: tuple[tuple[str, str], ...] = tuple(
    sorted(
        (
            ("水电收入", "utility_expense"),
            ("水电费用", "utility_expense"),
            ("水电支出", "utility_expense"),
            ("水电充值", "utility_expense"),
            ("运营支出", "operating_expenses"),
            ("完成月结", "write_request"),
            ("应该做什么", "next_step"),
            ("该做什么", "next_step"),
            ("核对结果", "reconciliation"),
            ("下一步", "next_step"),
            ("清洁", "cleaning_statement"),
            ("保洁", "cleaning_statement"),
            ("布草", "linen_statement"),
            ("携程", "ota_statement"),
            ("抖音", "ota_statement"),
            ("美团", "ota_statement"),
            ("去哪儿", "ota_statement"),
            ("同程", "ota_statement"),
            ("飞猪", "ota_statement"),
            ("账单", "ota_statement"),
            ("ota", "ota_statement"),
            ("这个月", "month"),
            ("本月", "month"),
            ("月结", "month"),
            ("对账", "reconciliation"),
            ("核对", "reconciliation"),
            ("差异", "reconciliation"),
            ("状态", "status"),
            ("进度", "status"),
            ("当前", "status"),
            ("现在", "status"),
            ("建议", "next_step"),
            ("请问", "query"),
            ("查看", "query"),
            ("看看", "query"),
            ("总结", "query"),
            ("处理", "query"),
            ("识别", "query"),
            ("上传", "query"),
            ("资料", "query"),
            ("文件", "query"),
            ("文档", "query"),
            ("是什么", "query"),
            ("怎么样", "query"),
            ("这是", "query"),
            ("这份", "query"),
            ("给你", "query"),
            ("发给", "query"),
            ("请", "query"),
            ("帮我", "query"),
            ("我", "query"),
            ("这个", "query"),
            ("一下", "query"),
            ("先", "query"),
            ("刚", "query"),
            ("把", "write_request"),
            ("所有", "write_request"),
            ("全部", "write_request"),
            ("订单", "write_request"),
            ("金额", "write_request"),
            ("账单值", "write_request"),
            ("修改", "write_request"),
            ("改成", "write_request"),
            ("删除", "write_request"),
            ("批准", "write_request"),
            ("审批", "write_request"),
            ("执行", "write_request"),
            ("导入", "write_request"),
            ("关账", "write_request"),
            ("结账", "write_request"),
            ("立刻", "write_request"),
            ("直接", "write_request"),
            ("都", "write_request"),
            ("并", "write_request"),
            ("的", "query"),
        ),
        key=lambda item: len(item[0]),
        reverse=True,
    )
)


def canonical_safe_intent(text: str) -> tuple[str, ...] | None:
    """Reduce user prose to an allowlisted concept vocabulary, or fail closed."""
    normalized = unicodedata.normalize("NFKC", text).lower()
    # Calendar fragments are reduced to the canonical month concept. This lets
    # ordinary phrases such as “8 月账单” work without persisting or sending the
    # user's raw numbers. Digits in amounts, cells or identifiers remain and
    # still fail closed below.
    normalized = re.sub(r"\d{4}\s*[-/]\s*\d{1,2}", "本月", normalized)
    normalized = re.sub(r"(?:\d{4}\s*年|\d{1,2}\s*(?:月|日|号))", "本月", normalized)
    # Filenames/cells and any unrecognized letters never cross a durable or
    # provider boundary. ``ota`` is the only allowed Latin token.
    if re.search(r"\d", normalized):
        return None
    remainder = normalized
    found: set[str] = set()
    while True:
        for phrase, token in _SAFE_INTENT_LEXEMES:
            position = remainder.find(phrase)
            if position >= 0:
                remainder = remainder[:position] + remainder[position + len(phrase) :]
                found.add(token)
                break
        else:
            break
    if re.sub(r"[\s?？!！,，。.、:：;；()（）\-_]+", "", remainder):
        return None
    if not found:
        return None
    return tuple(token for token in _SAFE_INTENT_ORDER if token in found)


def _deterministic_decision(
    intent_tokens: tuple[str, ...], attachment_ids: list[str]
) -> AssistantDecision:
    if "write_request" in intent_tokens:
        return AssistantDecision(intent="write_request")
    if attachment_ids:
        return AssistantDecision(intent="read_query", tool="get_document_status")
    for source_type in MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES:
        if source_type in intent_tokens:
            return AssistantDecision(
                intent="read_query",
                tool="get_source_status",
                arguments=ToolArguments(source_type=source_type),
            )
    if "next_step" in intent_tokens:
        return AssistantDecision(intent="read_query", tool="get_recommended_action")
    if "reconciliation" in intent_tokens:
        return AssistantDecision(
            intent="read_query", tool="explain_reconciliation_result"
        )
    if "status" in intent_tokens or "month" in intent_tokens:
        return AssistantDecision(intent="read_query", tool="get_month_status")
    return AssistantDecision(
        intent="clarification", clarification_code="choose_read_topic"
    )


def _safe_projection_context(projection: MonthlyCloseProjection) -> dict[str, Any]:
    return PromptProjectionDTO.model_validate(
        {
            "projection_version": projection.projection_version,
            "cycle_status": projection.cycle_status,
            "actor_role": projection.actor_role,
            "sources": [
                {"source_type": source.source_type, "state": source.state}
                for source in projection.sources
            ],
            "recommended_action": asdict(projection.recommended_action),
        }
    ).model_dump(mode="json")


def build_assistant_prompt(
    projection: MonthlyCloseProjection, intent_tokens: tuple[str, ...]
) -> str:
    context = json.dumps(
        _safe_projection_context(projection), ensure_ascii=False, separators=(",", ":")
    )
    return (
        "你是只读月结意图分类器。一次只能选择一个白名单查询或提出一个澄清问题。"
        "禁止写入、批准、执行、URL、SQL、任意字段和任意对象 ID。"
        f"白名单工具：{','.join(READ_TOOLS)}。"
        "澄清只能返回 clarification_code=choose_read_topic 或 select_attachment，不得返回问题正文。"
        "只返回符合 schema 的 JSON；模型只接收服务器归一化的安全意图。\n"
        f"角色投影：{context}\n安全意图：{json.dumps(intent_tokens, ensure_ascii=False, separators=(',', ':'))}"
    )


async def _classify_with_model(prompt: str) -> str:
    client = AsyncOpenAI(
        api_key=settings.DEEPSEEK_API_KEY,
        base_url="https://api.deepseek.com",
        timeout=12,
        max_retries=1,
    )
    response = await client.chat.completions.create(
        model="deepseek-chat",
        max_tokens=300,
        response_format={"type": "json_object"},
        messages=[{"role": "user", "content": prompt}],
    )
    content = response.choices[0].message.content
    if not content:
        raise ValueError("empty model response")
    return content


async def _validate_attachments(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    actor: Any,
    attachment_ids: list[str],
) -> list[dict[str, Any]]:
    if len(attachment_ids) > 10 or len(set(attachment_ids)) != len(attachment_ids):
        raise AssistantAttachmentNotFound
    actor_id, role = _actor_identity(actor)
    can_view_all_uploaders = role in {"admin", "finance", "operator"}
    result: list[dict[str, Any]] = []
    for attachment_id in attachment_ids:
        if not isinstance(attachment_id, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9._:-]{0,79}", attachment_id
        ):
            raise AssistantAttachmentNotFound
        document = await get_visible_projection_document(
            db, cycle, actor, attachment_id
        )
        if document is not None:
            view = document.to_dict()
            result.append(
                {
                    "document_id": view["document_id"],
                    "source_type": view["source_type"],
                    "storage_state": view["storage_state"],
                    "classification_state": view["classification_state"],
                    "analysis_state": view["analysis_state"],
                }
            )
            continue

        inbox_item = await db.scalar(
            select(MonthlyCloseInboxItem).where(
                MonthlyCloseInboxItem.cycle_id == cycle.cycle_id,
                MonthlyCloseInboxItem.item_id == attachment_id,
            )
        )
        if (
            inbox_item is None
            or inbox_item.status == "dismissed"
            or (not can_view_all_uploaders and inbox_item.created_by != actor_id)
        ):
            raise AssistantAttachmentNotFound
        if inbox_item.document_id is not None:
            archived_document = await get_visible_projection_document(
                db, cycle, actor, inbox_item.document_id
            )
            if archived_document is None:
                raise AssistantAttachmentNotFound
            view = archived_document.to_dict()
            result.append(
                {
                    "document_id": view["document_id"],
                    "source_type": view["source_type"],
                    "storage_state": view["storage_state"],
                    "classification_state": view["classification_state"],
                    "analysis_state": view["analysis_state"],
                }
            )
            continue
        result.append(
            {
                "item_id": inbox_item.item_id,
                "source_type": inbox_item.source_type,
                "storage_state": "stored",
                "classification_state": {
                    "received": "pending",
                    "needs_review": "needs_confirmation",
                    "classified": "confirmed",
                    "failed": "failed",
                }.get(inbox_item.status, "pending"),
                "analysis_state": "not_started",
                "inbox_state": inbox_item.status,
            }
        )
    return result


async def _ensure_conversation(
    db: AsyncSession,
    *,
    conversation_id: str,
    cycle_id: str,
    scope: str,
    actor_id: str,
) -> MonthlyCloseConversation:
    values = {
        "conversation_id": conversation_id,
        "cycle_id": cycle_id,
        "audience_scope": scope,
        "status": "active",
        "created_by": actor_id,
    }
    dialect = db.get_bind().dialect.name
    if dialect == "postgresql":
        statement = postgresql_insert(MonthlyCloseConversation).values(**values)
        # The table intentionally has both the conversation primary key and a
        # composite (conversation_id, cycle_id) identity used by child FKs.
        # Concurrent first turns can be arbitrated by either unique constraint,
        # so a target-specific ON CONFLICT is not sufficient on PostgreSQL.
        statement = statement.on_conflict_do_nothing()
        await db.execute(statement)
    elif dialect == "sqlite":
        statement = sqlite_insert(MonthlyCloseConversation).values(**values)
        statement = statement.on_conflict_do_nothing()
        await db.execute(statement)
    else:
        existing = await db.get(MonthlyCloseConversation, conversation_id)
        if existing is None:
            db.add(MonthlyCloseConversation(**values))
            await db.flush()
    conversation = await db.scalar(
        select(MonthlyCloseConversation).where(
            MonthlyCloseConversation.conversation_id == conversation_id,
            MonthlyCloseConversation.cycle_id == cycle_id,
        )
    )
    if conversation is None:
        raise RuntimeError("monthly-close conversation upsert failed")
    return conversation


async def _run_allowlisted_query(
    projection: MonthlyCloseProjection,
    decision: AssistantDecision,
    attachments: list[dict[str, Any]],
) -> dict[str, Any]:
    tool = decision.tool
    if tool == "get_month_status":
        return {
            "projection_version": projection.projection_version,
            "cycle_id": projection.cycle_id,
            "billing_month": projection.billing_month,
            "cycle_status": projection.cycle_status,
            "source_states": {
                source.source_type: source.state for source in projection.sources
            },
        }
    if tool == "get_source_status":
        source_type = decision.arguments.source_type
        source = next(
            (item for item in projection.sources if item.source_type == source_type),
            None,
        )
        return (
            {"source_type": source.source_type, "state": source.state}
            if source is not None
            else {"source_type": source_type, "state": "not_visible"}
        )
    if tool == "get_document_status":
        return {"documents": attachments}
    if tool == "get_issue_detail":
        action = projection.recommended_action
        return (
            {"issue": asdict(action)}
            if action.target_type == "issue"
            else {"issue": None, "reason": "no_visible_recommended_issue"}
        )
    if tool == "get_recommended_action":
        return {
            "projection_version": projection.projection_version,
            "recommended_action": asdict(projection.recommended_action),
        }
    if tool == "explain_reconciliation_result":
        return {
            "projection_version": projection.projection_version,
            "blocker_codes": projection.final_close_blockers,
        }
    raise ValueError("assistant decision did not select an allowlisted read query")


def _reply_text(decision: AssistantDecision, facts: dict[str, Any]) -> str:
    if decision.intent == "write_request":
        return "这项操作会改动财务记录，需要先生成方案并展示具体影响，再由有权限的人审批；月结助理不会直接执行写入。"
    if decision.intent == "sensitive_input":
        return "消息包含敏感信息，内容已隐藏且未发送给模型；请通过受控附件或月结工作台继续。"
    if decision.intent == "clarification":
        return CLARIFICATION_TEXT[decision.clarification_code or "choose_read_topic"]
    if decision.tool == "get_recommended_action":
        action = facts.get("recommended_action") or {}
        return f"当前下一步：{action.get('label', '请查看月结工作台')}。"
    if decision.tool == "get_source_status":
        labels = {"missing": "还没收到资料", "processing": "已收到，正在处理", "needs_action": "已收到，仍需识别或核对", "blocked": "仍有事项待处理", "completed": "资料已处理", "not_applicable": "本月不适用", "not_visible": "当前不可查看"}
        source = facts.get("source_type")
        return f"{LABELS.get(source, '这类资料')}：{labels.get(facts.get('state'), '待核实')}。" + (GUIDANCE.get(source, "") if facts.get("state") == "missing" else "")
    if decision.tool == "get_month_status":
        states = {"missing": "缺资料", "processing": "正在处理", "needs_action": "待识别或核对", "blocked": "有待处理事项", "completed": "资料已处理", "not_applicable": "本月不适用"}
        details = "；".join(f"{LABELS.get(key, '资料')}：{states.get(state, '待核实')}" for key, state in facts.get("source_states", {}).items())
        return f"{facts.get('billing_month', '本月')} 的资料进度：{details}。资料处理后还需完成月结复核。"
    if decision.tool == "get_document_status":
        documents = facts.get("documents") or []
        if any(item.get("storage_state") in {"failed", "missing"} for item in documents):
            return "文件没有完整保存。请查看对应文件的失败说明并重新上传；上传失败不代表资料已核对。"
        if facts.get("work_record_count"):
            return f"这份保洁文件已保存，已补齐 {facts['work_record_count']} 条历史打扫记录。打扫记录的处理结果仍然保留，可以打开下面的文件证据查看；保洁费用是否齐全需另行核对，你可以继续问“保洁费用还缺什么”。"
        inbox_documents = [item for item in documents if item.get("item_id")]
        if inbox_documents:
            if any(item.get("inbox_state") == "failed" for item in inbox_documents):
                return "文件仍然保留，但识别没有完成；请在资料记录中重新识别。"
            if any(
                item.get("inbox_state") == "needs_review" for item in inbox_documents
            ):
                source_types = {item.get("source_type") for item in inbox_documents}
                if len(source_types) == 1 and next(iter(source_types)):
                    source_label = _SOURCE_LABELS.get(
                        next(iter(source_types)), "月结资料"
                    )
                    return f"根据你的说明，这份文件可能是{source_label}；请确认后继续分析和核对。"
                return "文件已收到，但资料类型需要你确认；确认后就能继续分析和核对。"
            if any(item.get("inbox_state") == "classified" for item in inbox_documents):
                source_types = {item.get("source_type") for item in inbox_documents}
                if len(source_types) == 1:
                    source_label = _SOURCE_LABELS.get(
                        next(iter(source_types)), "月结资料"
                    )
                    return f"已识别为{source_label}，请确认资料类型；确认后系统会继续分析和核对。"
                return "文件类型已经识别，请确认资料类型；确认后系统会继续分析和核对。"
            return f"已收到 {len(inbox_documents)} 份资料，正在识别资料类型；完成后这里会显示下一步。"
        if any(
            item.get("analysis_state") in {"failed", "needs_mapping"}
            for item in documents
        ):
            return "这份文件已保存，但还没有读出可用于核对的明细。请在文件卡片中打开“调整读取方式”或识别结果，先确认表格结构；无需重新上传，当前没有补录或修改费用。"
        if any(
            item.get("analysis_state") in {"not_started", "queued", "running"}
            for item in documents
        ):
            return "这份文件已保存，识别尚未完成。请查看文件卡片的处理进度；当前没有补录或修改费用。"
        return "已按你当前权限查看所选附件状态；请通过文件卡片继续核对。"
    if decision.tool == "explain_reconciliation_result":
        return "这是当前权限范围内的对账事实和关账阻塞项。"
    return "这是当前月份的确定性状态；下一步以工作台建议为准。"


async def list_monthly_close_chat_history(db, cycle, actor):
    """Reload this administrator's verified turns without exposing private snapshots."""
    actor_id, role = _actor_identity(actor)
    from app.services.monthly_close.cleaning_work_import import _admin
    await _admin(db, actor_id)
    projection = await build_monthly_close_projection(db, cycle, actor)
    visible_ids = {doc.document_id for source in projection.sources for doc in source.documents}
    rows = await db.execute(
        select(MonthlyCloseRun, MonthlyCloseMessage)
        .join(MonthlyCloseMessage, MonthlyCloseMessage.run_id == MonthlyCloseRun.run_id)
        .where(MonthlyCloseRun.cycle_id == cycle.cycle_id,
               MonthlyCloseRun.actor_id == actor_id,
               MonthlyCloseRun.status.in_(["succeeded", "waiting_user", "degraded"]),
               MonthlyCloseMessage.role == "assistant")
        .order_by(MonthlyCloseRun.started_at.desc(), MonthlyCloseRun.run_id.desc()).limit(100)
    )
    replies = []
    for run, message in reversed(rows.all()):
        if run.permission_snapshot.get("role") != role:
            continue
        reply = _validated_durable_reply(run, message, expected_scope=_audience_scope(role))
        if reply.tool == "cleaning_work_chat" and reply.facts.get("document_id") not in visible_ids:
            continue
        if reply.tool == "formatted_result":
            from app.services.monthly_close.result_delivery import source_reply
            try:
                await source_reply(db, cycle, actor, reply.run_id)
            except (LookupError, PermissionError, ValueError):
                continue
        if reply.tool == "financial_case":
            try:
                await financial_case_chat._service().validate_attachments(
                    db, cycle.cycle_id, [source["source_id"] for source in reply.facts["sources"]], include_inactive=True
                )
            except (LookupError, PermissionError, ValueError):
                continue
        replies.append(reply.to_dict())
    return replies


async def _load_conversation_memory(db, cycle, actor_id):
    row = (await db.execute(select(MonthlyCloseRun, MonthlyCloseMessage).join(
        MonthlyCloseMessage, MonthlyCloseMessage.run_id == MonthlyCloseRun.run_id
    ).where(
        MonthlyCloseRun.cycle_id == cycle.cycle_id,
        MonthlyCloseRun.actor_id == actor_id,
        MonthlyCloseRun.status.in_(["succeeded", "degraded"]),
        MonthlyCloseRun.output_payload_redacted["tool"].as_string() == "conversation_memory",
        MonthlyCloseMessage.role == "assistant",
    ).order_by(MonthlyCloseRun.finished_at.desc(), MonthlyCloseRun.run_id.desc()).limit(1))).first()
    if not row or row[0].permission_snapshot.get("role") != "admin":
        return []
    reply = _validated_durable_reply(*row, expected_scope="finance")
    if reply.tool != "conversation_memory" or reply.facts.get("kind") != "conversation_memory":
        return None
    memory = ConversationMemoryFacts.model_validate(reply.facts)
    return memory.notes if memory.integrity_valid else None


async def _load_semantic_context(db, cycle, actor_id, projection, text, context_run_id):
    documents = [doc for source in projection.sources for doc in source.documents]
    doc_refs = {f"D{i}": doc.document_id for i, doc in enumerate(documents[:30])}
    reverse_docs = {value: key for key, value in doc_refs.items()}
    def aliased_question(question):
        # Resolve filenames locally; originals and possible personal names never leave here.
        names = {}
        for doc in documents[:30]:
            if doc.filename:
                names.setdefault(doc.filename, []).append(reverse_docs[doc.document_id])
        for name in sorted(names, key=len, reverse=True):
            question = question.replace(name, "资料" + "、".join(names[name]))
        return semantic_agent.provider_question(redact_text(question))
    rows = (await db.execute(
        select(MonthlyCloseRun, MonthlyCloseMessage)
        .join(MonthlyCloseMessage, MonthlyCloseMessage.run_id == MonthlyCloseRun.run_id)
        .where(MonthlyCloseRun.cycle_id == cycle.cycle_id,
               MonthlyCloseRun.actor_id == actor_id,
               MonthlyCloseRun.status.in_(["succeeded", "waiting_user", "degraded"]),
               MonthlyCloseMessage.role == "assistant")
        .order_by(MonthlyCloseRun.started_at.desc(), MonthlyCloseRun.run_id.desc()).limit(12)
    )).all()
    # Retrieve the newest prior state for each topic beyond the recent window.
    # Older turns remain reference evidence, never fresh business facts or approval.
    from sqlalchemy import func
    topic_key = func.coalesce(
        MonthlyCloseRun.output_payload_redacted["facts"]["focus"].as_string(),
        MonthlyCloseRun.output_payload_redacted["tool"].as_string(),
    )
    ranked = select(
        MonthlyCloseRun.run_id.label("id"),
        func.row_number().over(partition_by=topic_key, order_by=(MonthlyCloseRun.started_at.desc(), MonthlyCloseRun.run_id.desc())).label("position"),
    ).where(
        MonthlyCloseRun.cycle_id == cycle.cycle_id,
        MonthlyCloseRun.actor_id == actor_id,
        MonthlyCloseRun.status.in_(["succeeded", "waiting_user", "degraded"]),
    ).subquery()
    anchors = (await db.execute(select(MonthlyCloseRun, MonthlyCloseMessage).join(
        MonthlyCloseMessage, MonthlyCloseMessage.run_id == MonthlyCloseRun.run_id
    ).where(MonthlyCloseRun.run_id.in_(select(ranked.c.id).where(ranked.c.position == 1)),
            MonthlyCloseMessage.role == "assistant")
        .order_by(MonthlyCloseRun.started_at.desc(), MonthlyCloseRun.run_id.desc()).limit(20))).all()
    existing_ids = {run.run_id for run, _ in rows}
    rows.extend(row for row in anchors if row[0].run_id not in existing_ids)
    if context_run_id and context_run_id not in {run.run_id for run, _ in rows}:
        # An explicitly selected older card remains addressable without loading all history.
        older = (await db.execute(
            select(MonthlyCloseRun, MonthlyCloseMessage)
            .join(MonthlyCloseMessage, MonthlyCloseMessage.run_id == MonthlyCloseRun.run_id)
            .where(MonthlyCloseRun.run_id == context_run_id,
                   MonthlyCloseRun.cycle_id == cycle.cycle_id,
                   MonthlyCloseRun.actor_id == actor_id,
                   MonthlyCloseRun.status.in_(["succeeded", "waiting_user", "degraded"]),
                   MonthlyCloseMessage.role == "assistant")
        )).first()
        if older:
            rows.append(older)
    rows.sort(key=lambda row: (row[0].started_at, row[0].run_id), reverse=True)
    history, refs = [], {}
    for run, message in reversed(rows):
        if run.permission_snapshot.get("role") != "admin":
            continue
        reply = _validated_durable_reply(run, message, expected_scope="finance")
        # A format change preserves the original read scope for the next question.
        display_run_id = reply.run_id
        if reply.tool == "formatted_result":
            from app.services.monthly_close.result_delivery import source_reply
            try:
                reply = await source_reply(db, cycle, {"user_id": actor_id, "role": "admin"}, display_run_id)
            except (LookupError, PermissionError, ValueError):
                continue
        if reply.tool not in {"cleaning_work_chat", "review_month", "agent_clarification", "investigate_cleaning", "get_document_status", "order_identity_chat", "conversation_memory", "financial_case"}:
            continue
        if reply.tool == "financial_case":
            try:
                await financial_case_chat._service().validate_attachments(db, cycle.cycle_id, [s["source_id"] for s in reply.facts.get("sources", [])])
            except (LookupError, PermissionError, ValueError):
                continue
        facts = reply.facts
        doc_id = facts.get("document_id")
        if reply.tool == "get_document_status" and len(facts.get("documents", [])) == 1:
            doc_id = facts["documents"][0].get("document_id")
        if doc_id and doc_id not in reverse_docs:
            continue
        question = facts.get("request_text", "")
        if reply.tool == "order_identity_chat":
            # Identity values stay local; routing only needs the completed topic.
            question = "补充订单平台单号"
        elif reply.tool == "conversation_memory":
            question = "查看或更新本月已保存的处理原则"
        elif reply.tool == "financial_case":
            question = "查看财务资料的暂算报告" if facts.get("report_snapshot") else "核对财务资料"
        if _looks_sensitive(question):
            continue
        ref = f"H{len(history)}"
        item = {"ref":ref, "display_run_id":display_run_id, "question":aliased_question(question), "tool":reply.tool,
                "focus":facts.get("focus"), "query_mode":facts.get("query_mode"), "state":facts.get("state"),
                "document_ref":reverse_docs.get(doc_id)}
        if reply.tool == "order_identity_chat":
            item["focus"] = "order_integrity"
        if facts.get("amount_summary"):
            item["expense_categories"] = facts["amount_summary"].get("category_filter", [])
        if facts.get("investigation_scopes"):
            item["investigation_scopes"] = [
                {"topic": entry["topic"], "label": entry["label"], "count": entry["count"],
                 "document_ref": reverse_docs.get(entry.get("document_id"))}
                for entry in facts["investigation_scopes"]
                if not entry.get("document_id") or entry["document_id"] in reverse_docs
            ]
            item["detail_evidence_hash"] = facts.get("detail_evidence_hash")
            item["visible_ordinals"] = [row.get("ordinal") or index + 1 for index, row in enumerate(facts.get("detail_items") or [])]
        if reply.tool == "investigate_cleaning":
            item["focus"] = "cleaning_fees"
            selected_case = facts.get("selected") or {}
            item["selected_ordinal"] = selected_case.get("ordinal")
            item["selected_room"] = selected_case.get("room_id")
        if facts.get("spec"):
            item["selection"] = {key:value for key,value in facts["spec"].items() if key != "restore_ids"}
        if facts.get("comparison"):
            comparison = facts["comparison"]
            item["matched_count"] = comparison.get("matched_count")
            item["differences"] = [{key:row.get(key) for key in ("service_date", "room_ref", "service_type", "status", "table_count", "system_count")} for row in comparison.get("differences", [])[:20]]
        item["has_restorable_result"] = bool(facts.get("removal_ids"))
        refs[ref] = reply
        history.append(item)
    if context_run_id and context_run_id not in {item["display_run_id"] for item in history}:
        # A supplied reference must never silently fall back to another actor/old plan.
        raise ValueError("conversation reference is not available")
    context = {"billing_month":cycle.billing_month, "question":aliased_question(text),
               "history":history,
               "explicit_context":next((item["ref"] for item in history if item["display_run_id"] == context_run_id), None),
               "documents":[{"ref":ref, "source_type":doc.source_type} for ref,doc in zip(doc_refs, documents[:30], strict=True)],
               "source_states":{source.source_type:source.state for source in projection.sources}}
    for item in history:
        item.pop("display_run_id", None)
    notes = await _load_conversation_memory(db, cycle, actor_id)
    context["remembered_requirements"] = [
        {"topic": note.topic, "instruction": aliased_question(re.sub(r"ORD-[A-Za-z0-9-]+", "[系统订单]", order_identity_chat.local_privacy_text(note.text), flags=re.IGNORECASE))}
        for note in (notes or []) if not _looks_sensitive(note.text)
    ]
    context["context_policy"] = "历史仅提供话题和已表达的要求；当前问题优先，业务事实必须重新查询，保存的原则不能替代操作确认。"
    return context, doc_refs, refs


def _repeat_read_decision(text, refs, context_run_id):
    """Repeat an unchanged, verified read intent, never a stored answer or write.

    Only consecutive identical turns qualify. A topic change, a selected older
    card, an ambiguous clarification or any write breaks this chain.
    """
    replies = list(refs.values())
    if not replies or (context_run_id and replies[-1].run_id != context_run_id):
        return None
    for reply in reversed(replies):
        facts = reply.facts
        if facts.get("request_text", "").strip() != redact_text(text).strip():
            return None
        if reply.tool == "agent_clarification" and facts.get("retryable") is True:
            continue
        if reply.tool != "review_month" or reply.intent != "read_query":
            return None
        mode = facts.get("query_mode")
        # Legacy records lack mode: only explicit step queries can be replayed
        # without guessing whether the original asked for format/capabilities.
        if mode is None and facts.get("focus") not in {
            "order_integrity", "exception_clearance", "preflight",
            "settlement_review", "owner_confirmation",
        }:
            return None
        return semantic_agent.SemanticDecision(
            tool="review_month", focus=facts["focus"], mode=mode or "status"
        )
    return None


async def _answer_financial_case_message(
    db, cycle, actor, text, attachment_ids, context_run_id, *, progress,
):
    """Use the ordinary durable conversation and terminal hash for case evidence."""
    actor_id, role = _actor_identity(actor)
    if role != "admin":
        raise PermissionError("financial sources require an administrator")
    service = financial_case_chat._service()
    # Resolve the prior turn before writing the new receipt. Never substitute
    # whichever proposal happens to be newest inside the financial service.
    resolved_context = await financial_case_chat.resolve_context_run_id(
        db, cycle, actor, context_run_id
    )
    scope = _audience_scope(role)
    cycle_id = cycle.cycle_id
    conversation_id = _stable_id("MCCV-", f"{cycle_id}:{scope}")
    await _ensure_conversation(
        db, conversation_id=conversation_id, cycle_id=cycle_id,
        scope=scope, actor_id=actor_id,
    )
    durable_input = redact_text(text)
    run_id = "MCR-" + uuid4().hex[:20].upper()
    user_message_id = "MCM-" + uuid4().hex[:20].upper()
    run = MonthlyCloseRun(
        run_id=run_id, cycle_id=cycle_id, conversation_id=conversation_id,
        trigger_type="user_message", actor_type="user", actor_id=actor_id,
        permission_snapshot={"role": role, "projection_version": financial_case_chat.VERSION},
        status="running", model_provider=None, model_name=None,
        prompt_version=financial_case_chat.VERSION, tool_manifest_version=financial_case_chat.VERSION,
        input_hash=sha256(durable_input.encode("utf-8")).hexdigest(),
        started_at=datetime.now(timezone.utc),
    )
    db.add(run)
    await db.flush()
    db.add(MonthlyCloseMessage(
        message_id=user_message_id, cycle_id=cycle_id, conversation_id=conversation_id,
        run_id=run_id, role="user", content_redacted=durable_input,
        attachments=list(attachment_ids), visibility_scope=scope, created_by=actor_id,
    ))
    await append_cycle_event(
        db, cycle_id, "assistant.received", actor,
        {"message_id": user_message_id, "run_id": run_id},
        f"assistant-received:{user_message_id}",
    )
    await db.commit()

    async def recheck_permission():
        user = await db.scalar(select(User).where(User.user_id == actor_id).execution_options(populate_existing=True))
        if user is None or not user.is_active or str(getattr(user.role, "value", user.role)) != "admin":
            raise AssistantAuthorizationChanged
        current_cycle = await db.get(MonthlyCloseCycle, cycle_id, populate_existing=True)
        if current_cycle is None:
            raise AssistantAuthorizationChanged
        return current_cycle

    try:
        current_cycle = await recheck_permission()
        await service.validate_attachments(db, cycle_id, attachment_ids)
        locked_run = await db.scalar(select(MonthlyCloseRun).where(
            MonthlyCloseRun.run_id == run_id
        ).with_for_update().execution_options(populate_existing=True))
        if locked_run is None:
            raise RuntimeError("financial case run disappeared")
        if locked_run.status != "running":
            reply, _won = await _complete_run(
                db, run_id, reply=_safe_legacy_terminal_reply(locked_run),
                status="degraded", error_code="RUN_ALREADY_TERMINAL", error_detail_redacted=None,
            )
            return reply
        await progress("reading_evidence", "正在核对已上传资料和当前财务证据")
        facts = await service.respond(
            db, current_cycle, {"user_id": actor_id, "role": "admin"}, text,
            list(attachment_ids), context_run_id=resolved_context, run_id=run_id,
        )
        await recheck_permission()
        validated = FinancialCaseFacts.model_validate(facts)
        if validated.billing_month != current_cycle.billing_month:
            raise ValueError("financial case reply belongs to another cycle month")
        await service.validate_attachments(
            db, cycle_id, [source.source_id for source in validated.sources]
        )
        reply = AssistantReply(
            message=validated.message,
            intent="action_plan" if validated.state == "proposal" else "action_result" if validated.state == "completed" else "read_query",
            tool="financial_case", facts=validated.model_dump(mode="json"),
            recommended_action={}, narration_degraded=False,
            conversation_id=conversation_id, run_id=run_id,
        )
        reply = _attach_requested_output(reply, text)
        terminal_reply, _won = await _complete_run(
            db, run_id, reply=reply,
            status="waiting_user" if validated.state in {"proposal", "needs_information"} else "succeeded",
            error_code=None, error_detail_redacted=None,
        )
    except Exception as exc:
        await db.rollback()
        permission_changed = isinstance(exc, (PermissionError, AssistantAuthorizationChanged))
        terminal_reply, won = await _terminalize_degraded_run(
            db, run_id,
            error_code="AUTHORIZATION_CHANGED" if permission_changed else "FINANCIAL_CASE_FAILED",
            message="你的当前权限已变更，本次查询已安全终止。" if permission_changed else "本次财务资料核对未完成，请重新选择资料后重试。",
        )
        if permission_changed and won:
            raise AssistantAuthorizationChanged from exc
    await progress("completed", "结果已保存")
    return terminal_reply


async def answer_monthly_close_message(
    db: AsyncSession,
    cycle: MonthlyCloseCycle,
    actor: Any,
    text: str,
    attachment_ids: list[str],
    context_run_id: str | None = None,
    *, on_progress=None,
) -> AssistantReply:
    """Persist and answer one cycle-scoped, actor-attributed turn with bounded, confirmed actions."""
    actor_id, role = _actor_identity(actor)
    if not isinstance(text, str) or not text.strip() or len(text) > 4000:
        raise ValueError("assistant message must be between 1 and 4000 characters")
    async def progress(stage, message):
        if on_progress:
            await on_progress({"stage":stage, "message":message})

    await progress("received", "已收到，正在读取当前月份和对话记录")
    from app.services.monthly_close.result_output import formatting_only
    if role == "admin" and not attachment_ids and not _looks_sensitive(text) and formatting_only(text):
        from app.services.monthly_close.result_delivery import answer_formatted
        return await answer_formatted(db, cycle, actor, text, context_run_id)

    # Viewing a formatted plan must preserve the exact plan for a later explicit
    # confirmation. Resolve only an owned, integrity-checked formatting turn;
    # unavailable sources stay pinned so they cannot select an older plan.
    if role == "admin" and not attachment_ids:
        previous = await financial_case_chat.previous_reply(db, cycle, actor, context_run_id)
        if previous and previous.tool == "formatted_result":
            from app.services.monthly_close.result_delivery import source_reply
            context_run_id = previous.run_id
            try:
                context_run_id = (await source_reply(db, cycle, actor, previous.run_id)).run_id
            except (LookupError, PermissionError, ValueError):
                pass

    attachment_ids = await financial_case_chat.resolve_operation_attachment_ids(
        db, cycle, actor, text, attachment_ids, context_run_id
    )
    if await financial_case_chat.can_handle(db, cycle, actor, text, attachment_ids, context_run_id):
        return await _answer_financial_case_message(
            db, cycle, actor, text, attachment_ids, context_run_id, progress=progress
        )
    control = re.sub(r"[\s，。！!]+", "", text) in {
        "确认执行", "确认删除", "确认补齐", "确认处理", "确认恢复", "执行这个方案",
        "按这个方案执行", "确认", "通过", "取消", "取消方案", "先不执行", "先别执行", "不要执行",
    }
    use_semantic = (role == "admin" and settings.MONTHLY_CLOSE_SEMANTIC_AGENT_ENABLED
                    and not attachment_ids and not control and not _looks_sensitive(text))
    semantic_result = None
    semantic_error = None
    semantic_refs = {}
    semantic_docs = {}
    semantic_context = None
    semantic_selection = None
    semantic_inspect = False
    semantic_inspect_fees = False
    semantic_restore = False
    semantic_cancel = False
    semantic_explain = False
    repeated_read = False
    order_mode = False
    memory_command = conversation_context.memory_request(text) if role == "admin" and not attachment_ids and not _looks_sensitive(text) else None
    memory_mode = memory_command is not None
    previous_order = None
    previous_order_run_id = None
    order_target_hint = None
    attachments = await _validate_attachments(db, cycle, actor, attachment_ids)
    if role == "admin" and not attachment_ids and not _looks_sensitive(order_identity_chat.local_privacy_text(text)):
        previous_query = select(MonthlyCloseRun, MonthlyCloseMessage).join(
            MonthlyCloseMessage, MonthlyCloseMessage.run_id == MonthlyCloseRun.run_id
        ).where(MonthlyCloseRun.cycle_id == cycle.cycle_id, MonthlyCloseRun.actor_id == actor_id,
                MonthlyCloseRun.status.in_(["succeeded", "waiting_user", "degraded"]),
                MonthlyCloseMessage.role == "assistant")
        if context_run_id:
            previous_query = previous_query.where(MonthlyCloseRun.run_id == context_run_id)
        prior_order = (await db.execute(previous_query.order_by(MonthlyCloseRun.started_at.desc()).limit(1))).first()
        if prior_order and prior_order[0].permission_snapshot.get("role") == "admin":
            prior_reply = _validated_durable_reply(*prior_order, expected_scope="finance")
            if prior_reply.tool == "order_identity_chat":
                previous_order = OrderIdentityFacts.model_validate(prior_reply.facts)
                previous_order_run_id = prior_reply.run_id
                order_target_hint = previous_order.order_id
            elif prior_reply.tool == "review_month" and prior_reply.facts.get("focus") == "order_integrity":
                missing_ids = {item.get("order_id") for item in (prior_reply.facts.get("detail_items") or [])
                               if item.get("issue_code") == "missing_platform_order_id" and item.get("order_id")}
                if len(missing_ids) == 1 and prior_reply.facts.get("detail_total") == len(prior_reply.facts.get("detail_items") or []):
                    order_target_hint = next(iter(missing_ids))
        order_mode = order_identity_chat.wants_identity(text) or bool(previous_order and control)
        if order_mode:
            use_semantic = False
    month_route = (
        progress_request(text)
        if role == "admin" and not attachment_ids and not _looks_sensitive(text)
        and (canonical_safe_intent(text) is None or context_run_id)
        else None
    )
    if not month_route and role == "admin" and context_run_id and not attachment_ids and not _looks_sensitive(text) and re.search(r"为什么|怎么|格式|需要什么|传过|发过|已经传|已经发", text) and not re.search(r"删|执行|确认|批准|补齐|恢复|撤销|取消", text):
        context_run = await db.get(MonthlyCloseRun, context_run_id)
        if context_run and context_run.actor_id == actor_id and context_run.cycle_id == cycle.cycle_id and context_run.status == "succeeded" and context_run.permission_snapshot.get("role") == role:
            context_message = await db.get(MonthlyCloseMessage, _stable_id("MCM-", f"terminal:{context_run_id}"))
            if context_message:
                context_reply = _validated_durable_reply(context_run, context_message, expected_scope=_audience_scope(role))
                if context_reply.tool == "review_month":
                    month_route = context_reply.facts.get("focus") or "all"
    previous_work_chat = None
    previous_work_run_id = None
    work_chat = False
    work_document_ids = []
    if not month_route and role == "admin" and not _looks_sensitive(text) and not re.search(r"OTA|水电|业主|平台账单|收款|结算单|布草", text, re.IGNORECASE):
        previous_query = select(MonthlyCloseRun).where(
            MonthlyCloseRun.cycle_id == cycle.cycle_id,
            MonthlyCloseRun.actor_id == actor_id,
            MonthlyCloseRun.status == "succeeded",
            MonthlyCloseRun.tool_manifest_version == WORK_CHAT_VERSION,
        )
        if context_run_id:
            previous_query = previous_query.where(MonthlyCloseRun.run_id == context_run_id)
        prior = await db.scalar(previous_query.order_by(MonthlyCloseRun.started_at.desc(), MonthlyCloseRun.run_id.desc()).limit(1))
        if prior and prior.permission_snapshot.get("role") == role:
            terminal = await db.get(MonthlyCloseMessage, _stable_id("MCM-", f"terminal:{prior.run_id}"))
            if terminal:
                validated = _validated_durable_reply(prior, terminal, expected_scope=_audience_scope(role))
                if validated.tool == "cleaning_work_chat":
                    previous_work_chat = WorkChatFacts.model_validate(validated.facts)
                    previous_work_run_id = prior.run_id
        if previous_work_chat or re.search(r"保洁|打扫|按.{0,8}表|以.{0,8}表|系统.{0,8}删", text) or attachment_ids:
            from sqlalchemy.orm import undefer

            from app.models.monthly_close import MonthlyCloseDocument
            from app.services.billing_recon.parser import BillParseError
            from app.services.monthly_close.cleaning_work_log import (
                ServiceStatementError,
                parse_cleaning_work_log,
            )
            requested_ids = [item["document_id"] for item in attachments if item.get("document_id")]
            if not requested_ids and previous_work_chat and previous_work_chat.document_id:
                requested_ids = [previous_work_chat.document_id]
            docs_query = select(MonthlyCloseDocument).where(MonthlyCloseDocument.cycle_id == cycle.cycle_id, MonthlyCloseDocument.source_type == "cleaning_statement", MonthlyCloseDocument.is_active.is_(True)).options(undefer(MonthlyCloseDocument.content))
            if requested_ids: docs_query = docs_query.where(MonthlyCloseDocument.document_id.in_(requested_ids))
            for doc in await db.scalars(docs_query):
                try:
                    if parse_cleaning_work_log(doc.content, doc.filename, cycle.billing_month) is not None:
                        work_document_ids.append(doc.document_id)
                except (ServiceStatementError, BillParseError):
                    continue
            work_chat = bool(work_document_ids)
    # The investigation uses explicit business selectors and live server tools,
    # not the legacy model classifier. Context must belong to this exact actor,
    # cycle and role and pass the same durable-output integrity checks as replay.
    previous_investigation = None
    if not attachment_ids and not _looks_sensitive(text):
        query = select(MonthlyCloseRun).where(
            MonthlyCloseRun.cycle_id == cycle.cycle_id,
            MonthlyCloseRun.actor_id == actor_id,
            MonthlyCloseRun.status == "succeeded",
        )
        if context_run_id:
            query = query.where(MonthlyCloseRun.run_id == context_run_id)
        elif "保洁" in text or "打扫" in text:
            query = query.where(
                MonthlyCloseRun.tool_manifest_version == CLEANING_TOOL_VERSION
            )
        previous_run = await db.scalar(
            query.order_by(
                MonthlyCloseRun.started_at.desc(), MonthlyCloseRun.run_id.desc()
            ).limit(1)
        )
        if previous_run and previous_run.permission_snapshot.get("role") == role:
            previous_message = await db.get(
                MonthlyCloseMessage,
                _stable_id("MCM-", f"terminal:{previous_run.run_id}"),
            )
            if previous_message is not None:
                previous_reply = _validated_durable_reply(
                    previous_run, previous_message, expected_scope=_audience_scope(role)
                )
                if previous_reply.tool == "investigate_cleaning":
                    previous_investigation = CleaningInvestigationFacts.model_validate(
                        previous_reply.facts
                    )
    investigation_request = (
        parse_cleaning_request(text, has_context=previous_investigation is not None)
        if not attachment_ids and not _looks_sensitive(text) and not work_chat and not month_route
        else None
    )
    intent_tokens = canonical_safe_intent(text)
    unsafe_input = intent_tokens is None and investigation_request is None
    if investigation_request is not None:
        intent_tokens = ("cleaning_investigation",)
    if work_chat:
        intent_tokens, unsafe_input = ("cleaning_work_chat",), False
    if month_route:
        intent_tokens, unsafe_input = ("month_progress",), False
    if use_semantic:
        intent_tokens, unsafe_input = ("semantic_agent",), False
    if order_mode:
        month_route, work_chat, investigation_request = None, False, None
        intent_tokens, unsafe_input = ("order_identity_chat",), False
    if memory_mode:
        month_route, work_chat, investigation_request, order_mode, use_semantic = None, False, None, False, False
        intent_tokens, unsafe_input = ("conversation_memory",), False
    sensitive_input = unsafe_input and _looks_sensitive(text)
    if not unsafe_input and "write_request" not in intent_tokens and attachment_ids:
        source_hints = [
            source_type
            for source_type in MONTHLY_CLOSE_ACTIVE_SOURCE_TYPES
            if source_type in intent_tokens
        ]
        if len(source_hints) == 1:
            for attachment in attachments:
                if "item_id" not in attachment:
                    continue
                await apply_inbox_source_hint(
                    db,
                    cycle,
                    attachment["item_id"],
                    source_type=source_hints[0],
                    actor_id=actor_id,
                    actor_role=role,
                )
            attachments = await _validate_attachments(db, cycle, actor, attachment_ids)
    projection = await build_monthly_close_projection(db, cycle, actor)
    local_read_reference = None
    if use_semantic or (role == "admin" and not attachment_ids and not control and not memory_mode and not order_mode and not _looks_sensitive(text)):
        try:
            semantic_context, semantic_docs, semantic_refs = await _load_semantic_context(db, cycle, actor_id, projection, text, context_run_id)
            from app.services.monthly_close.investigation_context import read_reference
            if not semantic_agent.month_scope_question(cycle.billing_month, text, strict=True):
                local_read_reference = read_reference(text, semantic_context)
            if local_read_reference:
                use_semantic = True
                intent_tokens, unsafe_input = ("semantic_agent",), False
        except ValueError:
            semantic_error = "CONTEXT_UNAVAILABLE"
    durable_input = (
        "[敏感内容已隐藏]"
        if unsafe_input
        else "[安全意图:" + ",".join(intent_tokens) + "]"
    )
    if investigation_request is not None:
        durable_input = (
            "[保洁调查:"
            + investigation_request.model_dump_json(exclude_none=True)
            + "]"
        )
    if work_chat or month_route or use_semantic or order_mode or memory_mode:
        durable_input = redact_text(text)
    scope = _audience_scope(role)
    conversation_key = (
        f"{cycle.cycle_id}:{scope}:{actor_id}"
        if scope == "assigned_staff"
        else f"{cycle.cycle_id}:{scope}"
    )
    conversation_id = _stable_id("MCCV-", conversation_key)
    await _ensure_conversation(
        db,
        conversation_id=conversation_id,
        cycle_id=cycle.cycle_id,
        scope=scope,
        actor_id=actor_id,
    )

    run_id = "MCR-" + uuid4().hex[:20].upper()
    user_message_id = "MCM-" + uuid4().hex[:20].upper()
    run = MonthlyCloseRun(
        run_id=run_id,
        cycle_id=cycle.cycle_id,
        conversation_id=conversation_id,
        trigger_type="user_message",
        actor_type="user",
        actor_id=actor_id,
        permission_snapshot={
            "role": role,
            "projection_version": projection.projection_version,
        },
        status="running",
        model_provider="deepseek"
        if settings.MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED
        and investigation_request is None
        and not work_chat and not month_route
        else None,
        model_name="deepseek-chat"
        if settings.MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED
        and investigation_request is None
        and not work_chat and not month_route
        else None,
        prompt_version=ASSISTANT_PROMPT_VERSION,
        tool_manifest_version=PROGRESS_CHAT_VERSION if month_route else WORK_CHAT_VERSION if work_chat else CLEANING_TOOL_VERSION
        if investigation_request is not None
        else TOOL_MANIFEST_VERSION,
        input_hash=sha256(durable_input.encode("utf-8")).hexdigest(),
        started_at=datetime.now(timezone.utc),
    )
    # The message has an exact (run_id, cycle_id) FK.  Flush its immutable
    # parent first so PostgreSQL never has to infer ordering between detached
    # id-only ORM objects during the later event-lock autoflush.
    db.add(run)
    await db.flush()
    db.add(
        MonthlyCloseMessage(
            message_id=user_message_id,
            cycle_id=cycle.cycle_id,
            conversation_id=conversation_id,
            run_id=run_id,
            role="user",
            content_redacted=durable_input,
            attachments=list(attachment_ids),
            visibility_scope=scope,
            created_by=actor_id,
        )
    )
    await append_cycle_event(
        db,
        cycle.cycle_id,
        "assistant.received",
        actor,
        {"message_id": user_message_id, "run_id": run_id},
        f"assistant-received:{user_message_id}",
    )
    # Persist receipt durably, then release the cycle lock and transaction
    # before making an external model call.
    await db.commit()

    degraded = unsafe_input or not (
        settings.MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED and settings.DEEPSEEK_API_KEY
    )
    error_code: str | None = (
        "INPUT_NOT_ALLOWLISTED"
        if unsafe_input
        else "MODEL_DISABLED"
        if degraded
        else None
    )
    decision = (
        AssistantDecision(intent="sensitive_input")
        if sensitive_input
        else AssistantDecision(
            intent="clarification", clarification_code="choose_read_topic"
        )
        if unsafe_input
        else _deterministic_decision(intent_tokens, attachment_ids)
    )
    if investigation_request is not None:
        decision = AssistantDecision(intent="read_query", tool="investigate_cleaning")
        degraded, error_code = False, None
    if work_chat:
        decision = AssistantDecision(intent="read_query", tool="cleaning_work_chat")
        degraded, error_code = False, None
    if month_route:
        decision = AssistantDecision(intent="read_query", tool="review_month")
        degraded, error_code = False, None
    if memory_mode:
        decision = AssistantDecision(intent="read_query", tool="conversation_memory")
        degraded, error_code = False, None
    if order_mode:
        decision = AssistantDecision(intent="read_query", tool="order_identity_chat")
        degraded, error_code = False, None
        run.model_provider, run.model_name = None, None
        run.prompt_version = run.tool_manifest_version = order_identity_chat.VERSION
    if use_semantic:
        # All natural-language routing is selected once from verified multi-turn context.
        # Never let a model failure inherit a prior destructive specification.
        month_route, work_chat, investigation_request = None, False, None
        previous_work_chat, previous_work_run_id, work_document_ids = None, None, []
        if not semantic_error:
            await progress("understanding", "正在结合前文理解你的要求")
            try:
                scope_question = semantic_agent.month_scope_question(cycle.billing_month, text, strict=True)
                amount = conversation_context.amount_request(text, semantic_context["history"], semantic_context.get("explicit_context"))
                fee_review = semantic_agent.work_fee_review_request(text)
                semantic_result = semantic_agent.SemanticDecision(tool="clarify", question=scope_question) if scope_question else fee_review if fee_review else semantic_agent.SemanticDecision.model_validate(local_read_reference) if local_read_reference else semantic_agent.SemanticDecision.model_validate(amount) if amount else _repeat_read_decision(text, semantic_refs, context_run_id)
                repeated_read = semantic_result is not None and not scope_question
                if semantic_result is None:
                    semantic_result = semantic_agent.fee_repair_request(text)
                if semantic_result is None:
                    if not settings.MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED or not settings.DEEPSEEK_API_KEY:
                        raise RuntimeError("semantic model unavailable")
                    semantic_result = await semantic_agent.choose_tool(semantic_context)
            except asyncio.CancelledError:
                await asyncio.shield(_terminalize_degraded_run(db, run_id, error_code="MODEL_CANCELLED", message="本次理解已停止，没有执行新的修改。"))
                raise
            except Exception as error:
                semantic_error = semantic_agent.failure_code(error)
                fallback = conversation_context.read_fallback(text, semantic_context["history"])
                if fallback:
                    semantic_result = semantic_agent.SemanticDecision.model_validate(fallback)
        if semantic_result:
            if conversation_context.explanation_only(text) and semantic_result.tool in {"preview_cleaning", "preview_restore", "cancel_plan"}:
                semantic_result = semantic_agent.SemanticDecision(tool="inspect_cleaning", mode="explain", context_ref=semantic_result.context_ref, document_ref=semantic_result.document_ref)
            prior_reply = semantic_refs.get(semantic_result.context_ref)
            if prior_reply and prior_reply.tool == "cleaning_work_chat":
                previous_work_chat = WorkChatFacts.model_validate(prior_reply.facts)
                previous_work_run_id = prior_reply.run_id
            if prior_reply and prior_reply.tool == "investigate_cleaning":
                previous_investigation = CleaningInvestigationFacts.model_validate(prior_reply.facts)
            if semantic_result.document_ref:
                work_document_ids = [semantic_docs[semantic_result.document_ref]]
            elif previous_work_chat and previous_work_chat.document_id:
                work_document_ids = [previous_work_chat.document_id]
            if semantic_result.tool == "review_month":
                month_route = semantic_result.focus
                decision = AssistantDecision(intent="read_query", tool="review_month")
            elif semantic_result.tool == "inspect_document":
                decision = AssistantDecision(intent="read_query", tool="get_document_status")
            elif semantic_result.tool == "investigate_fees":
                investigation_request = CleaningInvestigationRequest(
                    action="select" if semantic_result.case_ordinal else "explain" if semantic_result.mode == "explain" and previous_investigation else "list",
                    ordinal=semantic_result.case_ordinal, room=semantic_result.room,
                )
                decision = AssistantDecision(intent="read_query", tool="investigate_cleaning")
            elif semantic_result.tool in {"inspect_cleaning", "inspect_work_fees", "preview_cleaning", "preview_restore", "cancel_plan"}:
                work_chat = True
                semantic_inspect = semantic_result.tool == "inspect_cleaning"
                semantic_inspect_fees = semantic_result.tool == "inspect_work_fees"
                semantic_restore = semantic_result.tool == "preview_restore"
                semantic_cancel = semantic_result.tool == "cancel_plan"
                semantic_explain = semantic_result.mode == "explain"
                semantic_selection = semantic_result.selection.model_dump() if semantic_result.selection else None
                decision = AssistantDecision(intent="read_query", tool="cleaning_work_chat")
            else:
                decision = AssistantDecision(intent="clarification", tool="agent_clarification", clarification_code="choose_read_topic")
            degraded, error_code = bool(semantic_error), semantic_error
        else:
            decision = AssistantDecision(intent="clarification", tool="agent_clarification", clarification_code="choose_read_topic")
            degraded, error_code = True, semantic_error
        # Record the actual model and resulting tool family for replay and plan freshness.
        run = await db.get(MonthlyCloseRun, run_id)
        run.model_provider = "deepseek" if not repeated_read and semantic_error != "CONTEXT_UNAVAILABLE" and settings.MONTHLY_CLOSE_ASSISTANT_MODEL_ENABLED and settings.DEEPSEEK_API_KEY else None
        run.model_name = settings.MONTHLY_CLOSE_AGENT_MODEL if run.model_provider else None
        run.prompt_version = semantic_agent.VERSION
        run.tool_manifest_version = WORK_CHAT_VERSION if work_chat else PROGRESS_CHAT_VERSION if month_route else CLEANING_TOOL_VERSION if investigation_request else semantic_agent.VERSION
        await db.commit()
    if decision.intent == "write_request" or (attachment_ids and not unsafe_input):
        # The fail-safe write boundary is deterministic and does not depend on
        # model narration. Attachment receipt/status queries are deterministic
        # for the same reason and never send raw user prose to a provider.
        degraded = False
        error_code = None
    if (
        decision.intent not in {"write_request", "sensitive_input"}
        and not degraded
        and not attachment_ids
        and investigation_request is None
        and not work_chat
        and not month_route
        and not use_semantic
        and not order_mode
        and not memory_mode
    ):
        try:
            raw_decision = await _classify_with_model(
                build_assistant_prompt(projection, intent_tokens)
            )
            decision = AssistantDecision.model_validate_json(raw_decision)
            if decision.tool is not None and decision.tool not in READ_TOOLS:
                raise _ModelOutputInvalid
            if decision.tool == "get_document_status" and not attachments:
                raise _ModelOutputInvalid
        except (ValidationError, _ModelOutputInvalid):
            degraded = True
            error_code = "MODEL_OUTPUT_INVALID"
            decision = _deterministic_decision(intent_tokens, attachment_ids)
        except asyncio.CancelledError:
            await asyncio.shield(
                _terminalize_degraded_run(
                    db,
                    run_id,
                    error_code="MODEL_CANCELLED",
                    message="本次查询已取消并安全终止，请重新发起只读查询。",
                )
            )
            raise
        except Exception:
            degraded = True
            error_code = "MODEL_UNAVAILABLE"
            decision = _deterministic_decision(intent_tokens, attachment_ids)

    current_user = await db.scalar(
        select(User)
        .where(User.user_id == actor_id)
        .execution_options(populate_existing=True)
    )
    current_role = (
        current_user.role.value
        if current_user is not None and current_user.is_active
        else None
    )
    if current_role != role:
        terminal_reply, won = await _terminalize_degraded_run(
            db,
            run_id,
            error_code="AUTHORIZATION_CHANGED",
            message="你的当前权限已变更，本次查询已安全终止，请重新打开月结工作台。",
        )
        if not won:
            return terminal_reply
        raise AssistantAuthorizationChanged

    current_cycle = await db.get(MonthlyCloseCycle, cycle.cycle_id)
    if current_cycle is None:
        terminal_reply, won = await _terminalize_degraded_run(
            db,
            run_id,
            error_code="CYCLE_UNAVAILABLE",
            message="当前月结周期不可用，本次查询已安全终止。",
        )
        if not won:
            return terminal_reply
        raise AssistantAuthorizationChanged
    current_actor = {"user_id": actor_id, "role": current_role}
    try:
        attachments = await _validate_attachments(
            db, current_cycle, current_actor, attachment_ids
        )
    except AssistantAttachmentNotFound:
        terminal_reply, won = await _terminalize_degraded_run(
            db,
            run_id,
            error_code="ATTACHMENT_AUTHORIZATION_CHANGED",
            message="附件已不可用，本次查询已安全终止，请重新选择附件。",
        )
        if not won:
            return terminal_reply
        raise
    projection = await build_monthly_close_projection(db, current_cycle, current_actor)

    # A reference to the one visible upload can be answered from its state even
    # when unfamiliar prose cannot enter the model. Never infer a write action
    # or pick one document when several are visible.
    if (
        decision.intent == "clarification"
        and not use_semantic
        and not attachment_ids
        and not sensitive_input
        and re.search(r"(?:这|刚).{0,24}(?:表|记录|文件|明细)|刚上传", text)
    ):
        visible_documents = [
            doc for source in projection.sources for doc in source.documents
        ]
        if len(visible_documents) == 1:
            attachments = await _validate_attachments(
                db, current_cycle, current_actor, [visible_documents[0].document_id]
            )
            decision = AssistantDecision(
                intent="read_query", tool="get_document_status"
            )

    await progress("planning" if work_chat and semantic_selection else "checking", "正在核对当前记录并准备可确认的方案" if work_chat and semantic_selection else "正在读取最新资料和核对结果")
    if memory_mode:
        if db.get_bind().dialect.name == "postgresql":
            from sqlalchemy import text as sql_text
            await db.execute(sql_text("SELECT pg_advisory_xact_lock(hashtext('monthly-close-conversation-memory'), hashtext(:scope))"),
                {"scope": f"{current_cycle.cycle_id}:{actor_id}"})
        notes = await _load_conversation_memory(db, current_cycle, actor_id)
        memory_facts = conversation_context.memory_answer(current_cycle.billing_month, redact_text(text),
            (memory_command[0], redact_text(memory_command[1])), notes)
        facts = memory_facts.model_dump(mode="json", exclude_none=True)
    elif order_mode:
        superseded_order = False
        if previous_order and previous_order.state == "proposal" and control:
            latest_order = (await db.execute(select(MonthlyCloseRun, MonthlyCloseMessage).join(
                MonthlyCloseMessage, MonthlyCloseMessage.run_id == MonthlyCloseRun.run_id
            ).where(MonthlyCloseRun.cycle_id == cycle.cycle_id, MonthlyCloseRun.actor_id == actor_id,
                    MonthlyCloseRun.tool_manifest_version == order_identity_chat.VERSION,
                    MonthlyCloseRun.status.in_(["succeeded", "waiting_user", "degraded"]),
                    MonthlyCloseMessage.role == "assistant")
                .order_by(MonthlyCloseRun.started_at.desc()).limit(1))).first()
            if latest_order and latest_order[0].run_id != previous_order_run_id:
                latest_reply = _validated_durable_reply(*latest_order, expected_scope="finance")
                superseded_order = latest_reply.facts.get("state") != "completed" or latest_reply.facts.get("order_id") != previous_order.order_id
        order_facts = OrderIdentityFacts(billing_month=current_cycle.billing_month, request_text=redact_text(text),
            state="conflict", message="这份方案已被后续操作替代，本次没有修改订单。请查看最新方案。") if superseded_order else await order_identity_chat.answer_order_identity(
                db, current_cycle, actor_id, text, previous_order, previous_order_run_id, order_target_hint)
        facts = order_facts.model_dump(mode="json", exclude_none=True)
        facts["request_text"] = redact_text(text)
    elif use_semantic and decision.tool == "agent_clarification":
        message = ("之前引用的对话或文件已经不可用，请重新选择要继续的文件或说明当前问题。" if semantic_error == "CONTEXT_UNAVAILABLE" else "理解服务暂时没有返回可靠结果。你的资料和已有处理结果仍保留，请稍后重试这句话；本次没有生成或执行新方案。" if semantic_error else semantic_result.question)
        facts = AgentClarificationFacts(request_text=redact_text(text), billing_month=current_cycle.billing_month, message=message, retryable=bool(semantic_error)).model_dump(mode="json")
    elif use_semantic and decision.tool == "get_document_status":
        attachments = await _validate_attachments(db, current_cycle, actor, work_document_ids)
        selected_document = next((doc for source in projection.sources for doc in source.documents if doc.document_id in work_document_ids), None)
        facts = DocumentStatusFacts(documents=attachments, request_text=redact_text(text), billing_month=current_cycle.billing_month, work_record_count=getattr(selected_document, "work_record_count", None)).model_dump(mode="json")
    elif month_route:
        previous_scope = semantic_refs.get(semantic_result.context_ref) if semantic_result else None
        facts = (await read_progress(db, current_cycle, projection, redact_text(text), month_route,
            mode=semantic_result.mode if semantic_result else None,
            expense_categories=semantic_result.expense_categories if semantic_result else None,
            document_ids=work_document_ids,
            ordinal=semantic_result.case_ordinal if semantic_result else None,
            expected_evidence_hash=previous_scope.facts.get("detail_evidence_hash") if previous_scope else None,
        )).model_dump(mode="json")
    elif work_chat:
        if not control and conversation_context.explanation_only(text):
            semantic_selection, semantic_inspect, semantic_restore, semantic_explain, semantic_cancel = None, True, False, True, False
        chat_facts = await answer_work_chat(db, current_cycle, actor_id, text, work_document_ids, previous_work_chat, previous_work_run_id, selection=semantic_selection, inspect_only=semantic_inspect, inspect_fees_only=semantic_inspect_fees, restore_only=semantic_restore, explain_only=semantic_explain, cancel_only=semantic_cancel)
        facts = chat_facts.model_dump(mode="json")
    elif investigation_request is not None:
        investigation = await investigate_cleaning(
            db,
            current_cycle,
            current_role,
            investigation_request,
            previous_investigation,
            ready_document_ids={
                document.document_id
                for source in projection.sources
                for document in source.documents
                if document.analysis_state == "ready"
            },
        )
        if investigation.selected is not None and current_role in {"admin", "finance"}:
            # Release the read transaction during provider latency. Re-authorize
            # and re-read evidence afterwards; stale model ordering is discarded.
            before_hash = investigation.selected.evidence_hash
            await db.commit()
            try:
                findings, steps, mode = await reason_over_evidence(
                    investigation, business_question(text)
                )
            except asyncio.CancelledError:
                await asyncio.shield(
                    _terminalize_degraded_run(
                        db,
                        run_id,
                        error_code="MODEL_CANCELLED",
                        message="本次调查已取消，请重新核查。",
                    )
                )
                raise
            refreshed_user = await db.scalar(
                select(User)
                .where(User.user_id == actor_id)
                .execution_options(populate_existing=True)
            )
            if (
                refreshed_user is None
                or not refreshed_user.is_active
                or refreshed_user.role.value != role
            ):
                await _terminalize_degraded_run(
                    db,
                    run_id,
                    error_code="AUTHORIZATION_CHANGED",
                    message="你的权限已变化，本次调查已停止。",
                )
                raise AssistantAuthorizationChanged
            await db.refresh(current_cycle)
            projection = await build_monthly_close_projection(
                db, current_cycle, current_actor
            )
            fresh = await investigate_cleaning(
                db,
                current_cycle,
                current_role,
                investigation_request,
                previous_investigation,
                ready_document_ids={
                    document.document_id
                    for source in projection.sources
                    for document in source.documents
                    if document.analysis_state == "ready"
                },
            )
            if fresh.selected is None or fresh.selected.evidence_hash != before_hash:
                from app.services.monthly_close.investigation_agent import (
                    evidence_catalog,
                )

                mode, steps = "fallback", []
                fresh.evidence_changed = True
                fresh.note_code, fresh.note_case_id = None, None
                if fresh.selected:
                    fresh.saved_notes = [
                        note
                        for note in fresh.saved_notes
                        if note.case_id != fresh.selected.case_id
                    ]
                findings = [
                    item for items in evidence_catalog(fresh).values() for item in items
                ]
                fresh.message = (
                    "调查期间依据发生了变化，已重新取数；请按当前依据继续核查。"
                )
            fresh.findings = [
                InvestigationFinding.model_validate(item) for item in findings
            ]
            fresh.investigation_steps = steps
            fresh.reasoning_mode = mode
            if investigation_request.action == "draft" and fresh.selected:
                fresh.correction_draft = await correction_draft(
                    db, current_cycle, fresh
                )
            investigation = CleaningInvestigationFacts.model_validate(
                fresh.model_dump()
            )
            run = await db.get(MonthlyCloseRun, run_id)
            run.prompt_version = AGENT_VERSION
            if mode in {"model", "fallback"}:
                run.model_provider, run.model_name = "deepseek", "deepseek-chat"
        facts: dict[str, Any] = investigation.model_dump(mode="json")
    elif decision.intent == "write_request":
        facts = {
            "projection_version": projection.projection_version,
            "write_performed": False,
            "safe_next_step": "create_proposal",
        }
    elif decision.intent == "sensitive_input":
        facts = {
            "projection_version": projection.projection_version,
            "sensitive_input": True,
        }
    elif decision.intent == "clarification":
        facts = {"projection_version": projection.projection_version}
    else:
        facts = await _run_allowlisted_query(projection, decision, attachments)
    message = (
        facts["message"]
        if investigation_request is not None or work_chat or month_route or order_mode or memory_mode or decision.tool == "agent_clarification"
        else redact_text(_reply_text(decision, facts))
    )
    recommendation = asdict(projection.recommended_action)
    reply = AssistantReply(
        message=message,
        intent=("action_plan" if facts["state"] == "proposal" else "action_result" if facts["state"] == "completed" else "read_query") if work_chat or order_mode else decision.intent,
        tool=decision.tool,
        facts=facts,
        recommended_action=recommendation,
        narration_degraded=degraded,
        conversation_id=conversation_id,
        run_id=run_id,
    )
    reply = _attach_requested_output(reply, text)
    terminal_status: Literal["succeeded", "waiting_user", "degraded"] = (
        "waiting_user"
        if decision.intent == "clarification"
        else "degraded"
        if degraded
        else "succeeded"
    )
    terminal_reply, _won = await _complete_run(
        db,
        run_id,
        reply=reply,
        status=terminal_status,
        error_code=error_code,
        error_detail_redacted="模型暂不可用" if error_code else None,
    )
    await progress("completed", "结果已保存")
    return terminal_reply
